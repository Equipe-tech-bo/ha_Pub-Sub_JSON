from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .services import async_register_services

async def async_setup(hass: HomeAssistant, config: dict):
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    hass.data.setdefault(DOMAIN, {})
    await async_register_services(hass)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    hass.data.pop(DOMAIN, None)
    return True
