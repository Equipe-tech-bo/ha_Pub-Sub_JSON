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
    DEFAULT_PING_INTERVAL,
    PING_PAYLOAD,
)


_LOGGER = logging.getLogger(__name__)

# ── Queue globale par entry ────────────────────────────────────────────── #
_write_queues: dict[str, asyncio.Queue] = {}


async def _json_writer(hass: HomeAssistant, json_path: str, queue: asyncio.Queue):
    while True:
        item = await queue.get()
        if item is None:
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
    return [cast(x.strip()) for x in raw.split(",") if x.strip()]


def _clean_empty(node):
    if not isinstance(node, dict):
        if node is None or node == "":
            return None
        return node
    cleaned = {}
    for key, value in node.items():
        result = _clean_empty(value)
        if result is not None:
            cleaned[key] = result
    return cleaned if cleaned else None


def _delete_in_dict(data: dict, keys: list):
    if not keys:
        return
    key = keys[0]
    if len(keys) == 1:
        data.pop(key, None)
        return
    if key in data and isinstance(data[key], dict):
        _delete_in_dict(data[key], keys[1:])
        if not data[key]:
            data.pop(key, None)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
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

    # ── Énigmes en état FERMETURE ────────────────────────────────────── #
    fermeture_set: set[str] = set()

    # ── Queue d'écriture ──────────────────────────────────────────────── #
    write_queue = asyncio.Queue()
    _write_queues[entry.entry_id] = write_queue

    writer_task = hass.loop.create_task(
        _json_writer(hass, json_path, write_queue),
        name=f"enigme_sync_writer_{entry.entry_id}"
    )

    # ── Variables PING ────────────────────────────────────────────────── #
    ping_task: asyncio.Task | None = None
    ping_running = False
    ping_interval = DEFAULT_PING_INTERVAL
    pending_pings: dict[str, asyncio.TimerHandle] = {}

    def _enigme_entity_id(enigme_prefix: str) -> str:
        """Convertit BR/CRYPTE/SOCLE_MAGIE → input_boolean.br_crypte_socle_magie_ping"""
        name = enigme_prefix.replace("/", "_").lower()
        return f"{PING_ENTITY_PREFIX}{name}{PING_ENTITY_SUFFIX}"

    async def _set_ping_entity(enigme_prefix: str, state: bool):
        entity_id = _enigme_entity_id(enigme_prefix)
        service = "turn_on" if state else "turn_off"
        _LOGGER.info(f"[EnigmeSync] SET {entity_id} → {service}")  # ← AJOUTE ÇA
        try:
            await hass.services.async_call(
                "input_boolean", service,
                {"entity_id": entity_id},
                blocking=True  # ← change à True pour détecter les erreurs
            )
        except Exception as e:
            _LOGGER.error(f"[EnigmeSync] ERREUR set {entity_id}: {e}")

    def _ping_timeout(enigme_prefix: str):
        """Appelé si aucun STATE reçu après PING_TIMEOUT."""
        pending_pings.pop(enigme_prefix, None)
        hass.async_create_task(_set_ping_entity(enigme_prefix, False))
        _LOGGER.warning(f"[EnigmeSync] TIMEOUT PING → {enigme_prefix}")

    async def mqtt_message_received(msg):
        topic = msg.topic
        payload = msg.payload
        parts = topic.split("/")
    
        if len(parts) < 3:
            enigme_prefix = "/".join(parts)
        else:
            enigme_prefix = "/".join(parts[:3])
    
        # ── Réponse au PING (TOUJOURS traité, même en FERMETURE) ──── #
        if topic.endswith("/STATE") and enigme_prefix in pending_pings:
            timer = pending_pings.pop(enigme_prefix)
            timer.cancel()
            hass.async_create_task(_set_ping_entity(enigme_prefix, True))
            _LOGGER.debug(f"[EnigmeSync] PONG ← {enigme_prefix} ({payload})")
    
        # ── Gestion FERMETURE ─────────────────────────────────────── #
        if topic.endswith("/STATE"):
            if payload == "FERMETURE":
                fermeture_set.add(enigme_prefix)
                return
            else:
                fermeture_set.discard(enigme_prefix)
    
        if enigme_prefix in fermeture_set:
            return
    
        if any(p in topic_blacklist for p in parts):
            return
    
        await write_queue.put((parts, payload))

    # ── SERVICE sync ──────────────────────────────────────────────────── #
    async def handle_sync(call):
        l1 = call.data.get("level1", "").strip()
        l2 = call.data.get("level2", "").strip()
        l3 = call.data.get("level3", "").strip()
        parts = [p for p in [l1, l2, l3] if p]

        await write_queue.join()
        data = await _async_load_json(hass, json_path)

        if parts:
            subtree = _get_nested(data, parts)
            if subtree is None:
                _LOGGER.warning(f"[EnigmeSync] Chemin introuvable : {'.'.join(parts)}")
                return
            base_topic = "/".join(parts)
        else:
            subtree = data
            base_topic = ""

        await _publish_recursive(hass, subtree, base_topic, action_depths, topic_blacklist)

    hass.services.async_register(DOMAIN, "sync", handle_sync)

    # ── PING périodique ───────────────────────────────────────────────── #
    async def _ping_all_enigmes():
        """Envoie PING et démarre un timer par énigme."""
        _LOGGER.info("[EnigmeSync] _ping_all_enigmes démarré")
        try:
            while ping_running:
                _LOGGER.info("[EnigmeSync] PING cycle début...")
                await write_queue.join()
                data = await _async_load_json(hass, json_path)
                _LOGGER.info(f"[EnigmeSync] JSON chargé, clés: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                await _ping_recursive_with_timeout(data, [])
                _LOGGER.info(f"[EnigmeSync] PING cycle terminé, sleep {ping_interval}s")
                await asyncio.sleep(ping_interval)
        except asyncio.CancelledError:
            _LOGGER.info("[EnigmeSync] _ping_all_enigmes annulé")
            raise
        except Exception as e:
            _LOGGER.error(f"[EnigmeSync] _ping_all_enigmes CRASH: {e}", exc_info=True)

    async def _ping_recursive_with_timeout(node, path_parts):
        if not isinstance(node, dict):
            _LOGGER.warning(f"[EnigmeSync] PING skip (not dict) at {'/'.join(path_parts)}: {node}")
            return
    
        for key, value in node.items():
            child_parts = path_parts + [key]
            child_depth = len(child_parts)
    
            _LOGGER.debug(f"[EnigmeSync] PING recurse: {'/'.join(child_parts)} depth={child_depth}")
    
            if child_depth in action_depths:
                enigme_prefix = "/".join(child_parts)
                action_topic = enigme_prefix + f"/{TOPIC_ACTION_SUFFIX}"
    
                old_timer = pending_pings.pop(enigme_prefix, None)
                if old_timer:
                    old_timer.cancel()
    
                await mqtt.async_publish(hass, action_topic, PING_PAYLOAD)
                _LOGGER.info(f"[EnigmeSync] PING → {action_topic}")
    
                timer = hass.loop.call_later(
                    PING_TIMEOUT,
                    _ping_timeout,
                    enigme_prefix
                )
                pending_pings[enigme_prefix] = timer
    
            else:
                await _ping_recursive_with_timeout(value, child_parts)

    # ── SERVICE start_ping ────────────────────────────────────────────── #
    async def handle_start_ping(call):
        nonlocal ping_task, ping_running, ping_interval

        ping_interval = call.data.get("interval", DEFAULT_PING_INTERVAL)

        if ping_running:
            _LOGGER.info("[EnigmeSync] PING déjà en cours, redémarrage...")
            ping_running = False
            if ping_task:
                ping_task.cancel()
                ping_task = None

        ping_running = True
        ping_task = hass.async_create_task(
            _ping_all_enigmes(),
            name="enigme_sync_ping"
        )
        _LOGGER.info(f"[EnigmeSync] PING démarré (intervalle: {ping_interval}s)")

    # ── SERVICE stop_ping ─────────────────────────────────────────────── #
    async def handle_stop_ping(call):
        nonlocal ping_running, ping_task

        ping_running = False
        if ping_task:
            ping_task.cancel()
            ping_task = None
        _LOGGER.info("[EnigmeSync] PING arrêté")

    hass.services.async_register(DOMAIN, "start_ping", handle_start_ping)
    hass.services.async_register(DOMAIN, "stop_ping", handle_stop_ping)

    # ── Nettoyage à l'unload ──────────────────────────────────────────── #
    async def _on_unload():
        nonlocal ping_running, ping_task
        # Arrêt du ping
        ping_running = False
        if ping_task:
            ping_task.cancel()
            ping_task = None
        # Arrêt du writer
        await write_queue.put(None)
        await asyncio.wait_for(writer_task, timeout=5.0)
        _write_queues.pop(entry.entry_id, None)
        _LOGGER.debug("[EnigmeSync] Writer arrêté proprement")

    entry.async_on_unload(_on_unload)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True  # ← maintenant APRÈS tout l'enregistrement


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.services.async_remove(DOMAIN, "sync")
    hass.services.async_remove(DOMAIN, "start_ping")
    hass.services.async_remove(DOMAIN, "stop_ping")
    return True


# ── PUBLICATION RECURSIVE ─────────────────────────────────────────────── #
async def _publish_recursive(hass, node, base_topic: str, action_depths: list, topic_blacklist: list):
    if not isinstance(node, dict):
        return

    current_depth = len(base_topic.split("/")) if base_topic else 0

    if current_depth in action_depths:
        await _publish_action(hass, node, base_topic, topic_blacklist)
        return

    for key, value in node.items():
        if key in topic_blacklist:
            continue
        child_topic = f"{base_topic}/{key}" if base_topic else key
        depth = len(child_topic.split("/"))
        if depth in action_depths:
            await _publish_action(hass, value, child_topic, topic_blacklist)
        else:
            await _publish_recursive(hass, value, child_topic, action_depths, topic_blacklist)


async def _publish_action(hass, node: dict, enigme_topic: str, topic_blacklist: list):
    if not isinstance(node, dict):
        return

    action_topic = f"{enigme_topic}/{TOPIC_ACTION_SUFFIX}"

    filtered = {}
    for key, value in node.items():
        if key in topic_blacklist:
            continue
        if isinstance(value, dict):
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


# ── PING RECURSIVE (hors setup_entry) ────────────────────────────────── #
async def _ping_recursive(hass, node, path_parts, action_depths, topic_blacklist):
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        child_parts = path_parts + [key]
        child_depth = len(child_parts)
        if child_depth in action_depths:
            enigme_topic = "/".join(child_parts) + f"/{TOPIC_ACTION_SUFFIX}"
            _LOGGER.debug(f"[EnigmeSync] PING → {enigme_topic}")
            await mqtt.async_publish(hass, enigme_topic, PING_PAYLOAD)
        else:
            await _ping_recursive(hass, value, child_parts, action_depths, topic_blacklist)
