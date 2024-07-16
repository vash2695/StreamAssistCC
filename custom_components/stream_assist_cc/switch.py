import logging
from typing import Callable

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .core import run_forever, init_entity, EVENTS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([StreamAssistCCSwitch(config_entry)])


class StreamAssistCCSwitch(SwitchEntity):
    on_close: Callable | None = None

    def __init__(self, config_entry: ConfigEntry):
        self._attr_is_on = False
        self._attr_should_poll = False

        self.options = config_entry.options.copy()
        self.uid = init_entity(self, "mic", config_entry)

    def event_callback(self, event: PipelineEvent):
        # Event type: wake_word-start, wake_word-end
        # Error code: wake-word-timeout, wake-provider-missing, wake-stream-failed
        code = (
            event.data["code"]
            if event.type == PipelineEventType.ERROR
            else event.type.replace("_word", "")
        )

        name, state = code.split("-", 1)

        async_dispatcher_send(self.hass, f"{self.uid}-{name}", state, event.data)

    async def async_added_to_hass(self) -> None:
        self.options["assist"] = {"device_id": self.device_entry.id}

    async def async_turn_on(self) -> None:
        if self._attr_is_on:
            return

        self._attr_is_on = True
        self._async_write_ha_state()

        for event in EVENTS:
            async_dispatcher_send(self.hass, f"{self.uid}-{event}", None)

        try:
            self.on_close = run_forever(
                self.hass,
                self.options,
                context=self._context,
                event_callback=self.event_callback,
            )
        except Exception as e:
            _LOGGER.error(f"Error turning on StreamAssistCC: {e}")
            self._attr_is_on = False
            self._async_write_ha_state()

    async def async_turn_off(self) -> None:
        if not self._attr_is_on:
            return

        self._attr_is_on = False
        self._async_write_ha_state()

        if self.on_close is not None:
            try:
                await self.on_close()
            except Exception as e:
                _LOGGER.error(f"Error closing StreamAssistCC: {e}")
            finally:
                self.on_close = None

    async def async_will_remove_from_hass(self) -> None:
        if self._attr_is_on and self.on_close is not None:
            try:
                await self.on_close()
            except Exception as e:
                _LOGGER.error(f"Error closing StreamAssistCC during removal: {e}")
            finally:
                self.on_close = None
