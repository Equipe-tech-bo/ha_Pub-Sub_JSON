import json
import logging
import os

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

    # Chemin absolu
    if not json_path.startswith("/"):
        json_path = "/" + json_path

    # Blacklist — topics à ignorer à la réception
    raw_blacklist = (
        entry.options.get("topic_blacklist") or
        entry.data.get("topic_blacklist") or
        ", ".join(DEFAULT_TOPIC_BLACKLIST)
    )
    topic_blacklist = _parse_list_str(raw_blacklist)   # ["ACTION","NETWORK","OTA"]

    # Profondeurs ACTION — ex: "3" → [3]  /  "3, 4" → [3, 4]
    raw_depths = (
        entry.options.get("action_depths") or
        entry.data.get("action_depths") or
        ", ".join(str(d) for d in DEFAULT_ACTION_DEPTHS)
    )
    action_depths = _parse_list_str(raw_depths, cast=int)  # [3] ou [3, 4]

    _LOGGER.info(f"[EnigmeSync] MQTT filter    : {mqtt_filter}")
    _LOGGER.info(f"[EnigmeSync] JSON path      : {json_path}")
    _LOGGER.info(f"[EnigmeSync] Blacklist      : {topic_blacklist}")
    _LOGGER.info(f"[EnigmeSync] Action depths  : {action_depths}")

    _ensure_json_file(json_path)

    # ── CALLBACK MQTT ─────────────────────────────────────────────────── #
    async def mqtt_message_received(msg):
        topic   = msg.topic
        payload = msg.payload
    
        # Blacklist : ignore si un segment du topic est dans la liste
        parts = topic.split("/")
        if any(p in topic_blacklist for p in parts):
            _LOGGER.debug(f"[EnigmeSync] Topic ignoré (blacklist) : {topic}")
            return
    
        _LOGGER.debug(f"[EnigmeSync] Reçu [{topic}] : {payload}")
    
        data = _load_json(json_path)
    
        if payload == "" or payload is None:
            # Supprime la clé et remonte nettoyer les parents vides
            _delete_in_dict(data, parts)
        else:
            _set_nested(data, parts, payload)
    
        # Nettoyage récursif des nœuds vides restants
        cleaned = _clean_empty(data)
        if cleaned is None:
            cleaned = {}
    
        _save_json(json_path, cleaned)
    
    await mqtt.async_subscribe(hass, mqtt_filter, mqtt_message_received)


    # ── SERVICE sync ──────────────────────────────────────────────────── #
    async def handle_sync(call):
        l1 = call.data.get("level1", "").strip()
        l2 = call.data.get("level2", "").strip()
        l3 = call.data.get("level3", "").strip()
    
        # Reconstruit le path depuis les levels, ignore les vides
        parts = [p for p in [l1, l2, l3] if p]
    
        data = _load_json(json_path)
    
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
def _ensure_json_file(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({}, f, indent=4)


def _load_json(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def _set_nested(d: dict, keys: list, value):
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _get_nested(d: dict, keys: list):
    for key in keys:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return None
    return d
