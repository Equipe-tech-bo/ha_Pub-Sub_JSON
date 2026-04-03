import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from ..const import DOMAIN, MQTT_BASE_TOPIC

SERVICE_SYNC = "sync"

SYNC_SCHEMA = vol.Schema({
    vol.Required("path"): str,
})

async def async_register_services(hass: HomeAssistant):
    """Enregistre tous les services."""

    async def handle_sync(call: ServiceCall):
        path = call.data["path"]
        topic = f"{MQTT_BASE_TOPIC}/{path.replace('.', '/')}/set"
        # Ta logique MQTT ici
        await hass.components.mqtt.async_publish(hass, topic, "", qos=1)

    hass.services.async_register(DOMAIN, SERVICE_SYNC, handle_sync, schema=SYNC_SCHEMA)
