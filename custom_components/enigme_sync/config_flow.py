import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    DOMAIN,
    DEFAULT_JSON_PATH,
    DEFAULT_MQTT_FILTER,
    DEFAULT_TOPIC_BLACKLIST,
    DEFAULT_ACTION_DEPTHS,
)


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
            ): str,
            vol.Required(
                "json_path",
                default=DEFAULT_JSON_PATH,
            ): str,
            vol.Optional(
                "topic_blacklist",
                default=", ".join(DEFAULT_TOPIC_BLACKLIST),
            ): str,
            vol.Optional(
                "action_depths",
                default=", ".join(str(d) for d in DEFAULT_ACTION_DEPTHS),
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

        # Lecture de la valeur actuelle : options en priorité, sinon data initiale
        def _get(key, default):
            return self._config_entry.options.get(
                key,
                self._config_entry.data.get(key, default)
            )

        schema = vol.Schema({
            vol.Required(
                "mqtt_filter",
                default=_get("mqtt_filter", DEFAULT_MQTT_FILTER),
            ): str,
            vol.Required(
                "json_path",
                default=_get("json_path", DEFAULT_JSON_PATH),
            ): str,
            vol.Optional(
                "topic_blacklist",
                default=_get(
                    "topic_blacklist",
                    ", ".join(DEFAULT_TOPIC_BLACKLIST)
                ),
            ): str,
            vol.Optional(
                "action_depths",
                default=_get(
                    "action_depths",
                    ", ".join(str(d) for d in DEFAULT_ACTION_DEPTHS)
                ),
            ): str,
        })

        return self.async_show_form(
            step_id="init",
            data_schema=schema
        )
