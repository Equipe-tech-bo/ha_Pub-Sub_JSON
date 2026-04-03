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
        _set_nested(data, parts, payload)
        _save_json(json_path, data)

    await mqtt.async_subscribe(hass, mqtt_filter, mqtt_message_received)

    # ── SERVICE sync ──────────────────────────────────────────────────── #
    async def handle_sync(call: ServiceCall):
        path_param = call.data.get("path", "").strip()

        data = _load_json(json_path)

        if path_param:
            keys = path_param.split(".")
            subtree = _get_nested(data, keys)
            if subtree is None:
                _LOGGER.warning(f"[EnigmeSync] Chemin introuvable dans le JSON : {path_param}")
                return
            base_topic = "/".join(keys)
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
    """
    Parcourt le JSON et publie sur ENIGME/ACTION
    uniquement aux profondeurs configurées.

    Exemple avec action_depths = [3] et base = "BR" :
      BR/CRYPTE/SOCLE_MAGIE  →  profondeur 3  →  publie ACTION ✓
      BR/CRYPTE/LOT/ENIGME   →  profondeur 4  →  non publié   ✗

    Avec action_depths = [3, 4] les deux sont publiés.
    """
    if not isinstance(node, dict):
        return

    for key, value in node.items():

        # Ignore les clés blacklistées
        if key in topic_blacklist:
            continue

        child_topic = f"{base_topic}/{key}" if base_topic else key
        depth       = len(child_topic.split("/"))

        if depth in action_depths:
            # On est à la bonne profondeur → on publie les données de ce nœud
            await _publish_action(hass, value, child_topic, topic_blacklist)
        else:
            # On descend récursivement
            await _publish_recursive(hass, value, child_topic, action_depths, topic_blacklist)


async def _publish_action(hass, node, enigme_topic: str, topic_blacklist: list):
    """
    Publie sur ENIGME/ACTION le payload de sync
    pour toutes les données du nœud.

    topic  : BR/CRYPTE/SOCLE_MAGIE
    publie : BR/CRYPTE/SOCLE_MAGIE/ACTION
    payload: sync "DATA": { "VALIDE": "1" }
    """
    if not isinstance(node, dict):
        return

    action_topic = f"{enigme_topic}/{TOPIC_ACTION_SUFFIX}"

    for data_key, data_value in node.items():

        # Ignore les clés blacklistées
        if data_key in topic_blacklist:
            continue

        if isinstance(data_value, dict):
            # Plusieurs valeurs sous DATA → on les sérialise
            inner = ", ".join(
                f'"{k}": "{v}"'
                for k, v in data_value.items()
                if k not in topic_blacklist
            )
            payload = f'{SYNC_PAYLOAD_PREFIX} "{data_key}": {{ {inner} }}'
        else:
            payload = f'{SYNC_PAYLOAD_PREFIX} "{data_key}": "{data_value}"'

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
