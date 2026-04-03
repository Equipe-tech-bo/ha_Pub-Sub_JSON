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
)


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    # Récupère la config (options prioritaires sur data)
    mqtt_filter = entry.options.get("mqtt_filter") or entry.data.get("mqtt_filter")
    json_path   = entry.options.get("json_path")   or entry.data.get("json_path")

    # Sécurité : valeurs par défaut si toujours None
    if not mqtt_filter:
        mqtt_filter = DEFAULT_MQTT_FILTER
        _LOGGER.warning("[EnigmeSync] mqtt_filter non trouvé, utilisation de la valeur par défaut")
    # Force le chemin absolu
    if json_path and not json_path.startswith("/"):
        json_path = "/" + json_path
        _LOGGER.warning(f"[EnigmeSync] json_path corrigé : {json_path}")
    if not json_path:
        json_path = DEFAULT_JSON_PATH
        _LOGGER.warning("[EnigmeSync] json_path non trouvé, utilisation de la valeur par défaut")

    _LOGGER.info(f"[EnigmeSync] Abonnement MQTT sur : {mqtt_filter}")
    _LOGGER.info(f"[EnigmeSync] Fichier JSON : {json_path}")
    _LOGGER.debug(f"[EnigmeSync] entry.data    = {entry.data}")
    _LOGGER.debug(f"[EnigmeSync] entry.options = {entry.options}")


    # Création du fichier JSON s'il n'existe pas
    _ensure_json_file(json_path)

    # ------------------------------------------------------------------ #
    #  CALLBACK MQTT — Stockage automatique dans le JSON                  #
    # ------------------------------------------------------------------ #
    async def mqtt_message_received(msg):
        topic   = msg.topic          # ex: BR/CRYPTE/SOCLE_MAGIE/DATA/VALIDE
        payload = msg.payload        # ex: "1"

        _LOGGER.debug(f"[EnigmeSync] Reçu [{topic}] : {payload}")

        # Décompose le topic en liste de clés
        keys = topic.split("/")      # ["BR","CRYPTE","SOCLE_MAGIE","DATA","VALIDE"]

        # Charge le JSON existant
        data = _load_json(json_path)

        # Insère / met à jour la valeur dans l'arbre
        _set_nested(data, keys, payload)

        # Sauvegarde
        _save_json(json_path, data)

        _LOGGER.debug(f"[EnigmeSync] JSON mis à jour → {keys} = {payload}")

    # Abonnement MQTT
    await mqtt.async_subscribe(hass, mqtt_filter, mqtt_message_received)

    # ------------------------------------------------------------------ #
    #  SERVICE HA — Republication vers MQTT                               #
    # ------------------------------------------------------------------ #
    async def handle_sync(call: ServiceCall):
        """
        Service enigme_sync.sync
        Paramètre optionnel 'path' : ex 'BR', 'BR.CRYPTE', 'BR.CRYPTE.SOCLE_MAGIE'
        Si absent → republier tout le JSON
        """
        path_param = call.data.get("path", "")  # ex: "BR.CRYPTE"

        data = _load_json(json_path)

        if path_param:
            # Navigue jusqu'au nœud demandé
            keys = path_param.split(".")
            subtree = _get_nested(data, keys)
            if subtree is None:
                _LOGGER.warning(f"[EnigmeSync] Path introuvable dans le JSON : {path_param}")
                return
            base_topic = "/".join(keys)
        else:
            subtree    = data
            base_topic = ""

        # Publie récursivement
        await _publish_recursive(hass, subtree, base_topic)

    hass.services.async_register(DOMAIN, "sync", handle_sync)

    # Rechargement si options modifiées
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Recharge l'intégration si la config change."""
    await hass.config_entries.async_reload(entry.entry_id)


# ------------------------------------------------------------------ #
#  PUBLICATION RÉCURSIVE                                              #
# ------------------------------------------------------------------ #
async def _publish_recursive(hass: HomeAssistant, node, base_topic: str):
    """
    Parcourt l'arbre JSON et publie chaque feuille.

    Pour un nœud feuille à BR/CRYPTE/SOCLE_MAGIE/DATA/VALIDE = "1"
    → publie sur  BR/CRYPTE/SOCLE_MAGIE/ACTION
    → payload     sync "DATA": { "VALIDE": "1" }
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child_topic = f"{base_topic}/{key}" if base_topic else key
            await _publish_recursive(hass, value, child_topic)
    else:
        # On est sur une feuille
        # base_topic = BR/CRYPTE/SOCLE_MAGIE/DATA/VALIDE
        parts = base_topic.split("/")

        if len(parts) < 2:
            _LOGGER.warning(f"[EnigmeSync] Topic trop court pour reconstruire ACTION : {base_topic}")
            return

        # Le topic ACTION = tout sauf les 2 derniers segments + /ACTION
        # ex: BR/CRYPTE/SOCLE_MAGIE  +  /ACTION
        enigme_parts   = parts[:-2]          # ["BR","CRYPTE","SOCLE_MAGIE"]
        data_key       = parts[-2]           # "DATA"
        value_key      = parts[-1]           # "VALIDE"

        action_topic   = "/".join(enigme_parts) + f"/{TOPIC_ACTION_SUFFIX}"

        # Construit le payload : sync "DATA": { "VALIDE": "1" }
        payload = f'{SYNC_PAYLOAD_PREFIX} "{data_key}": {{ "{value_key}": "{node}" }}'

        _LOGGER.info(f"[EnigmeSync] Publish [{action_topic}] : {payload}")

        await mqtt.async_publish(hass, action_topic, payload)


# ------------------------------------------------------------------ #
#  UTILITAIRES JSON                                                   #
# ------------------------------------------------------------------ #
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
    """Insère value dans d en suivant la liste de clés."""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _get_nested(d: dict, keys: list):
    """Retourne le sous-arbre correspondant au chemin de clés."""
    for key in keys:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return None
    return d
