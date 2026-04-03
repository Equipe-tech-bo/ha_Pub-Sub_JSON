import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, DEFAULT_JSON_PATH, DEFAULT_MQTT_FILTER

class EnigmeSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow pour Enigme Sync."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            return self.async_create_entry(
                title="Enigme Sync",
                data=user_input
            )

        schema = vol.Schema({
            vol.Required(
                "mqtt_filter",
                default=DEFAULT_MQTT_FILTER,
                description="Pattern d'abonnement MQTT (ex: BR/#)"
            ): str,
            vol.Required(
                "json_path",
                default=DEFAULT_JSON_PATH,
                description="Chemin du fichier JSON (ex: config/www/log/session.json)"
            ): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EnigmeSyncOptionsFlow(config_entry)


class EnigmeSyncOptionsFlow(config_entries.OptionsFlow):
    """Options flow pour modifier la config après installation."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema({
            vol.Required(
                "mqtt_filter",
                default=self._config_entry.options.get(
                    "mqtt_filter",
                    self._config_entry.data.get("mqtt_filter", DEFAULT_MQTT_FILTER)
                )
            ): str,
            vol.Required(
                "json_path",
                default=self._config_entry.options.get(
                    "json_path",
                    self._config_entry.data.get("json_path", DEFAULT_JSON_PATH)
                )
            ): str,
        })

        return self.async_show_form(
            step_id="init",
            data_schema=schema
        )
