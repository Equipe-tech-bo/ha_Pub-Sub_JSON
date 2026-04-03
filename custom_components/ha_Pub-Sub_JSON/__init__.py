import json
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import mqtt

_LOGGER = logging.getLogger(__name__)

DOMAIN = "enigme_sync"
JSON_PATH = "/config/enigmes.json"


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up enigme_sync."""

    def _load_json() -> dict:
        p = Path(JSON_PATH)
        if p.exists():
            return json.loads(p.read_text())
        return {}

    def _save_json(data: dict):
        Path(JSON_PATH).write_text(json.dumps(data, indent=2))

    def _set_nested(data: dict, keys: list[str], value):
        """Set a value in a nested dict, creating intermediate dicts."""
        for key in keys[:-1]:
            data = data.setdefault(key, {})
        data[keys[-1]] = value

    def _get_nested(data: dict, keys: list[str]):
        """Get a value from nested dict. Returns None if missing."""
        for key in keys:
            if isinstance(data, dict):
                data = data.get(key)
            else:
                return None
        return data

    def _collect_enigmes(data, prefix_parts: list[str]) -> list[tuple[list[str], dict]]:
        """
        Parcourt récursivement et retourne les feuilles (énigmes).
        Une feuille = un dict qui contient 'etat' ou qui n'a aucune valeur dict.
        """
        results = []
        if not isinstance(data, dict):
            return results
        # C'est une énigme (feuille) si elle contient 'etat'
        if "etat" in data:
            results.append((prefix_parts, data))
        else:
            for key, val in data.items():
                results.extend(_collect_enigmes(val, prefix_parts + [key]))
        return results

    # --- Listener MQTT : écoute +/+/+/ACTION ---
    async def _mqtt_received(msg):
        """
        Topic format: LIEU/SALLE/ENIGME/ACTION
        Payload JSON: {"etat": "...", ...}
        """
        try:
            parts = msg.topic.split("/")
            if len(parts) != 4 or parts[3] != "ACTION":
                return

            lieu, salle, enigme = parts[0], parts[1], parts[2]
            payload = json.loads(msg.payload)

            data = _load_json()
            current = _get_nested(data, [lieu, salle, enigme])
            if current is None:
                current = {}

            # Merge le payload dans l'état existant
            current.update(payload)
            _set_nested(data, [lieu, salle, enigme], current)
            _save_json(data)

            _LOGGER.info("Saved %s/%s/%s: %s", lieu, salle, enigme, current)

        except Exception as e:
            _LOGGER.error("enigme_sync MQTT error: %s", e)

    await mqtt.async_subscribe(hass, "+/+/+/ACTION", _mqtt_received)

    # --- Service sync ---
    async def handle_sync(call: ServiceCall):
        """
        path: "BR" | "BR.CRYPTE" | "BR.CRYPTE.BOITE_PONCTION"
        Republish depuis le JSON vers MQTT.
        """
        path = call.data.get("path", "")
        keys = path.split(".")
        data = _load_json()

        node = _get_nested(data, keys)
        if node is None:
            _LOGGER.warning("Path '%s' not found in JSON", path)
            return

        enigmes = _collect_enigmes(node, keys)

        for parts, state in enigmes:
            topic = "/".join(parts) + "/ACTION"
            payload = json.dumps(state)
            await mqtt.async_publish(hass, topic, payload, retain=False)
            _LOGGER.info("Synced %s: %s", topic, payload)

    hass.services.async_register(DOMAIN, "sync", handle_sync)

    return True
