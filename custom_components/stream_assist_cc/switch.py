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
    _LOGGER.debug("Setting up StreamAssistCCSwitch")
    async_add_entities([StreamAssistCCSwitch(config_entry)])

class StreamAssistCCSwitch(SwitchEntity):
    on_close: Callable | None = None

    def __init__(self, config_entry: ConfigEntry):
        _LOGGER.debug("Initializing StreamAssistCCSwitch")
        self._attr_is_on = False
        self._attr_should_poll = False
        self.options = config_entry.options.copy()
        self.uid = init_entity(self, "mic", config_entry)
        _LOGGER.debug(f"StreamAssistCCSwitch initialized with UID: {self.uid}")

    def event_callback(self, event: PipelineEvent):
        _LOGGER.debug(f"Received pipeline event: {event.type}")
        code = (
            event.data["code"]
            if event.type == PipelineEventType.ERROR
            else event.type.replace("_word", "")
        )
        name, state = code.split("-", 1)
        _LOGGER.debug(f"Dispatching event: {self.uid}-{name}, state: {state}")
        self.hass.loop.call_soon_threadsafe(
            async_dispatcher_send, self.hass, f"{self.uid}-{name}", state, event.data
        )

    async def async_added_to_hass(self) -> None:
        _LOGGER.debug("StreamAssistCCSwitch added to HASS")
        self.options["assist"] = {"device_id": self.device_entry.id}
        _LOGGER.debug(f"Set device_id in options: {self.device_entry.id}")

    async def async_turn_on(self) -> None:
        _LOGGER.debug("Attempting to turn on StreamAssistCCSwitch")
        if self._attr_is_on:
            _LOGGER.debug("StreamAssistCCSwitch is already on")
            return

        self._attr_is_on = True
        self._async_write_ha_state()
        _LOGGER.debug("Set StreamAssistCCSwitch state to on")

        for event in EVENTS:
            _LOGGER.debug(f"Dispatching initial event: {self.uid}-{event}")
            async_dispatcher_send(self.hass, f"{self.uid}-{event}", None)

        try:
            _LOGGER.debug("Calling run_forever")
            self.on_close = run_forever(
                self.hass,
                self.options,
                context=self._context,
                event_callback=self.event_callback,
            )
            _LOGGER.debug("run_forever completed successfully")
        except Exception as e:
            _LOGGER.error(f"Error turning on StreamAssistCC: {e}")
            self._attr_is_on = False
            self._async_write_ha_state()

    async def async_turn_off(self) -> None:
        _LOGGER.debug("Attempting to turn off StreamAssistCCSwitch")
        if not self._attr_is_on:
            _LOGGER.debug("StreamAssistCCSwitch is already off")
            return

        self._attr_is_on = False
        self._async_write_ha_state()
        _LOGGER.debug("Set StreamAssistCCSwitch state to off")

        if self.on_close is not None:
            try:
                _LOGGER.debug("Calling on_close function")
                self.on_close()  # Changed from await self.on_close()
                _LOGGER.debug("on_close function completed successfully")
            except Exception as e:
                _LOGGER.error(f"Error closing StreamAssistCC: {e}")
            finally:
                self.on_close = None
                _LOGGER.debug("Reset on_close to None")

    async def async_will_remove_from_hass(self) -> None:
        _LOGGER.debug("StreamAssistCCSwitch is being removed from HASS")
        if self._attr_is_on and self.on_close is not None:
            try:
                _LOGGER.debug("Calling on_close function during removal")
                self.on_close()  # Changed from await self.on_close()
                _LOGGER.debug("on_close function completed successfully during removal")
            except Exception as e:
                _LOGGER.error(f"Error closing StreamAssistCC during removal: {e}")
            finally:
                self.on_close = None
                _LOGGER.debug("Reset on_close to None during removal")
