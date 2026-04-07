import json
import logging
import os
import asyncio

from collections import deque
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import mqtt

from .const import (
    DOMAIN,
    TOPIC_ACTION_SUFFIX,
    SYNC_PAYLOAD_PREFIX,
    DEFAULT_MQTT_FILTER,
    DEFAULT_JSON_PATH,
    DEFAULT_TOPIC_BLACKLIST,
    DEFAULT_ACTION_DEPTHS,
)


_LOGGER = logging.getLogger(__name__)

# ── Queue globale par entry ────────────────────────────────────────────── #
_write_queues: dict[str, asyncio.Queue] = {}

# ── Cooldown anti-FERMETURE ───────────────────────────────────────────── #
_fermeture_cooldown: dict[str, float] = {}  # topic_prefix → timestamp fin cooldown
FERMETURE_COOLDOWN_MS = 3.0  # 3s pour avoir de la marge

async def _json_writer(hass: HomeAssistant, json_path: str, queue: asyncio.Queue):
    """
    Coroutine unique qui traite les écritures JSON une par une.
    Tourne en permanence tant que l'entry est chargée.
    """
    while True:
        item = await queue.get()

        if item is None:            # Signal d'arrêt propre
            queue.task_done()
            break

        parts, payload = item

        try:
            data = await _async_load_json(hass, json_path)

            if payload == "" or payload is None:
                _delete_in_dict(data, parts)
            else:
                _set_nested(data, parts, payload)

            cleaned = _clean_empty(data) or {}
            await _async_save_json(hass, json_path, cleaned)

            _LOGGER.debug(f"[EnigmeSync] Écrit [{'/'.join(parts)}] = {payload}")

        except Exception as e:
            _LOGGER.error(f"[EnigmeSync] Erreur écriture JSON : {e}")

        finally:
            queue.task_done()

def _parse_list_str(raw: str, cast=str) -> list:
    """Convertit une chaîne 'A, B, C' en liste typée."""
    return [cast(x.strip()) for x in raw.split(",") if x.strip()]

def _clean_empty(node):
    """
    Supprime récursivement les clés dont la valeur est vide ou ne contient
    que des valeurs vides (dict vide, string vide, None).
    Retourne None si le nœud entier est vide après nettoyage.
    """
    if not isinstance(node, dict):
        # Valeur scalaire : vide si None ou string vide
        if node is None or node == "":
            return None
        return node

    cleaned = {}
    for key, value in node.items():
        result = _clean_empty(value)
        if result is not None:
            cleaned[key] = result

    # Si le dict est vide après nettoyage, on retourne None
    return cleaned if cleaned else None

def _delete_in_dict(data: dict, keys: list):
    """
    Supprime la clé terminale dans le dict imbriqué selon le chemin keys.
    Nettoie ensuite les parents devenus vides.
    """
    if not keys:
        return

    key = keys[0]

    if len(keys) == 1:
        # Clé terminale : on supprime
        data.pop(key, None)
        return

    if key in data and isinstance(data[key], dict):
        _delete_in_dict(data[key], keys[1:])
        # Si le sous-dict est maintenant vide, on supprime le parent aussi
        if not data[key]:
            data.pop(key, None)
            
async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge l'entrée si les options sont modifiées."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    # ── Config ────────────────────────────────────────────────────────── #
    mqtt_filter = (
        entry.options.get("mqtt_filter") or
        entry.data.get("mqtt_filter") or
        DEFAULT_MQTT_FILTER
    )
    json_path = (
        entry.options.get("json_path") or
        entry.data.get("json_path") or
        DEFAULT_JSON_PATH
    )

    if not json_path.startswith("/"):
        json_path = "/" + json_path

    raw_blacklist = (
        entry.options.get("topic_blacklist") or
        entry.data.get("topic_blacklist") or
        ", ".join(DEFAULT_TOPIC_BLACKLIST)
    )
    topic_blacklist = _parse_list_str(raw_blacklist)

    raw_depths = (
        entry.options.get("action_depths") or
        entry.data.get("action_depths") or
        ", ".join(str(d) for d in DEFAULT_ACTION_DEPTHS)
    )
    action_depths = _parse_list_str(raw_depths, cast=int)

    _LOGGER.info(f"[EnigmeSync] MQTT filter    : {mqtt_filter}")
    _LOGGER.info(f"[EnigmeSync] JSON path      : {json_path}")
    _LOGGER.info(f"[EnigmeSync] Blacklist      : {topic_blacklist}")
    _LOGGER.info(f"[EnigmeSync] Action depths  : {action_depths}")

    await _async_ensure_json_file(hass, json_path)

    # ── Queue d'écriture ──────────────────────────────────────────────── #
    write_queue = asyncio.Queue()
    _write_queues[entry.entry_id] = write_queue

    # Lance la coroutine d'écriture en tâche de fond
    writer_task = hass.async_create_task(
        _json_writer(hass, json_path, write_queue),
        name=f"enigme_sync_writer_{entry.entry_id}"
    )

    # ── CALLBACK MQTT ─────────────────────────────────────────────────── #
    async def mqtt_message_received(msg):
        topic = msg.topic
        payload = msg.payload
    
        import time
    
        # ── Extraire le préfixe de l'énigme (tout sauf le dernier segment) ── #
        parts = topic.split("/")
        enigme_prefix = "/".join(parts[:-1])  # ex: BR/CRYPTE/SOCLE_OR
    
        now = time.monotonic()
    
        # ── Détection FERMETURE → démarre cooldown ────────────────────────── #
        if topic.endswith("/STATE") and payload == "FERMETURE":
            _LOGGER.debug(f"[EnigmeSync] FERMETURE détecté sur {topic}, cooldown {FERMETURE_COOLDOWN_MS*1000:.0f}ms")
            _fermeture_cooldown[enigme_prefix] = now + FERMETURE_COOLDOWN_MS
            return  # On n'écrit pas FERMETURE dans le JSON
    
        # ── Si l'énigme est en cooldown → on ignore ───────────────────────── #
        if enigme_prefix in _fermeture_cooldown:
            if now < _fermeture_cooldown[enigme_prefix]:
                _LOGGER.debug(f"[EnigmeSync] Ignoré (cooldown FERMETURE) : {topic} = {payload}")
                return
            else:
                # Cooldown expiré, on nettoie
                del _fermeture_cooldown[enigme_prefix]
    
        # ── Blacklist FERMETURE sur les topics STATE ─────────────────────── #
        if topic.endswith("/STATE") and payload == "FERMETURE":
            _LOGGER.debug(f"[EnigmeSync] Ignoré : {topic} = FERMETURE (protection anti-reboot)")
            return  # On n'écrit pas dans le JSON

        parts = topic.split("/")
        if any(p in topic_blacklist for p in parts):
            _LOGGER.debug(f"[EnigmeSync] Topic ignoré (blacklist) : {topic}")
            return

        _LOGGER.debug(f"[EnigmeSync] Reçu [{topic}] : {payload}")
        _LOGGER.debug(f"[EnigmeSync] Parts : {parts}")
        
        # On empile dans la queue, on n'écrit plus directement
        await write_queue.put((parts, payload))

    await mqtt.async_subscribe(hass, mqtt_filter, mqtt_message_received)

    # ── SERVICE sync ──────────────────────────────────────────────────── #
    async def handle_sync(call):
        l1 = call.data.get("level1", "").strip()
        l2 = call.data.get("level2", "").strip()
        l3 = call.data.get("level3", "").strip()

        parts = [p for p in [l1, l2, l3] if p]

        # Attend que la queue soit vide avant de lire
        # pour être sûr d'avoir le JSON à jour
        await write_queue.join()

        data = await _async_load_json(hass, json_path)

        if parts:
            subtree = _get_nested(data, parts)
            if subtree is None:
                _LOGGER.warning(f"[EnigmeSync] Chemin introuvable dans le JSON : {'.'.join(parts)}")
                return
            base_topic = "/".join(parts)
        else:
            subtree    = data
            base_topic = ""

        await _publish_recursive(hass, subtree, base_topic, action_depths, topic_blacklist)

    hass.services.async_register(DOMAIN, "sync", handle_sync)

    # ── Nettoyage a l'unload ───────────────────────────────────────────── #
    async def _on_unload():
        # Envoie le signal d'arrêt à la coroutine writer
        await write_queue.put(None)
        await asyncio.wait_for(writer_task, timeout=5.0)
        _write_queues.pop(entry.entry_id, None)
        _LOGGER.debug("[EnigmeSync] Writer arrêté proprement")

    entry.async_on_unload(_on_unload)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "sync")
    return True


# ── PUBLICATION RECURSIVE ─────────────────────────────────────────────── #
async def _publish_recursive(
    hass,
    node,
    base_topic: str,
    action_depths: list,
    topic_blacklist: list,
):
    if not isinstance(node, dict):
        return

    # Vérifie si on est déjà à la bonne profondeur
    current_depth = len(base_topic.split("/")) if base_topic else 0

    if current_depth in action_depths:
        # On est exactement sur le nœud cible → on publie directement
        await _publish_action(hass, node, base_topic, topic_blacklist)
        return

    for key, value in node.items():

        if key in topic_blacklist:
            continue

        child_topic = f"{base_topic}/{key}" if base_topic else key
        depth       = len(child_topic.split("/"))

        if depth in action_depths:
            await _publish_action(hass, value, child_topic, topic_blacklist)
        else:
            await _publish_recursive(hass, value, child_topic, action_depths, topic_blacklist)


async def _publish_action(hass, node: dict, enigme_topic: str, topic_blacklist: list):
    """
    Publie sur ENIGME/ACTION toutes les données du nœud enigme en un seul message.

    Exemple de nœud :
    {
        "DATA": { "VALIDE": "1", "TEST": "0" },
        "STATUS": "SOLVED"
    }

    Publie :
        BR/CRYPTE/SOCLE_MAGIE/ACTION → sync { "DATA": { "VALIDE": "1", "TEST": "0" }, "STATUS": "SOLVED" }
    """
    if not isinstance(node, dict):
        return

    action_topic = f"{enigme_topic}/{TOPIC_ACTION_SUFFIX}"

    # On filtre les clés blacklistées et on reconstruit un dict propre
    filtered = {}
    for key, value in node.items():
        if key in topic_blacklist:
            continue

        if isinstance(value, dict):
            # Filtre aussi les sous-clés blacklistées
            sub_filtered = {
                sub_key: sub_val
                for sub_key, sub_val in value.items()
                if sub_key not in topic_blacklist
            }
            if sub_filtered:
                filtered[key] = sub_filtered
        else:
            filtered[key] = value

    if not filtered:
        return

    # Sérialisation en JSON compact
    payload = f"{SYNC_PAYLOAD_PREFIX} {json.dumps(filtered, ensure_ascii=False)}"

    _LOGGER.info(f"[EnigmeSync] Publish [{action_topic}] : {payload}")
    await mqtt.async_publish(hass, action_topic, payload)


# ── UTILITAIRES JSON ──────────────────────────────────────────────────── #
async def _async_load_json(hass, path: str) -> dict:
    def _read():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
    return await hass.async_add_executor_job(_read)


async def _async_save_json(hass, path: str, data: dict):
    def _write():
        with open(path, "w") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    await hass.async_add_executor_job(_write)


async def _async_ensure_json_file(hass, path: str):
    def _ensure():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump({}, f, indent=4)
    await hass.async_add_executor_job(_ensure)


def _set_nested(d: dict, keys: list, value):
    for key in keys[:-1]:
        existing = d.get(key)
        # Si la valeur existante n'est pas un dict (ex: string), on l'écrase
        if not isinstance(existing, dict):
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value



def _get_nested(d: dict, keys: list):
    for key in keys:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return None
    return d
