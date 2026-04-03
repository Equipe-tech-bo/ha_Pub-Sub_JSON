"""Config flow for Enigme Sync."""
from homeassistant import config_entries
from .const import DOMAIN


class EnigmeSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Enigme Sync."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Enigme Sync", data={})

        return self.async_show_form(step_id="user")
