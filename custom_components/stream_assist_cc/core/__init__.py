import asyncio
import io
import logging
import time
import re
from typing import Callable
from mutagen.mp3 import MP3

from homeassistant.components import assist_pipeline
from homeassistant.components import media_player
from homeassistant.components import stt
from homeassistant.components.assist_pipeline import (
    AudioSettings,
    Pipeline,
    PipelineEvent,
    PipelineEventCallback,
    PipelineEventType,
    PipelineInput,
    PipelineStage,
    PipelineRun,
    WakeWordSettings,
)
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Context
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import Entity, DeviceInfo
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.network import get_url
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .stream import Stream

_LOGGER = logging.getLogger(__name__)

DOMAIN = "stream_assist_cc"
EVENTS = ["wake", "stt", "intent", "tts"]

CANCELLATION_PHRASES = [
    r'\bnevermind\b', r'\bnever mind\b', r'\bthank you\b', r'\bcancel that\b', 
    r'\bcancel\b', r'\babort\b', r'\bquit\b', r'\bexit\b', r'\bend\b', r'\bforget it\b', 
    r'\bthat\'s all\b', r'\bthat is all\b'
]


def new(cls, kwargs: dict):
    if not kwargs:
        return cls()
    kwargs = {k: v for k, v in kwargs.items() if hasattr(cls, k)}
    return cls(**kwargs)


def init_entity(entity: Entity, key: str, config_entry: ConfigEntry) -> str:
    unique_id = config_entry.entry_id[:7]
    num = 1 + EVENTS.index(key) if key in EVENTS else 0

    entity._attr_unique_id = f"{unique_id}-{key}"
    entity._attr_name = config_entry.title + " " + key.upper().replace("_", " ")
    entity._attr_icon = f"mdi:numeric-{num}"
    entity._attr_device_info = DeviceInfo(
        name=config_entry.title,
        identifiers={(DOMAIN, unique_id)},
        entry_type=DeviceEntryType.SERVICE,
    )

    return unique_id


async def get_stream_source(hass: HomeAssistant, entity: str) -> str | None:
    try:
        component: EntityComponent = hass.data["camera"]
        camera: Camera = next(e for e in component.entities if e.entity_id == entity)
        return await camera.stream_source()
    except Exception as e:
        _LOGGER.error("get_stream_source", exc_info=e)
        return None


def play_media(hass: HomeAssistant, entity_id: str, media_id: str, media_type: str):
    service_data = {
        "entity_id": entity_id,
        "media_content_id": media_player.async_process_play_media_url(hass, media_id),
        "media_content_type": media_type,
    }

    # hass.services.call will block Hass
    coro = hass.services.async_call("media_player", "play_media", service_data)
    hass.async_create_background_task(coro, "stream_assist_cc_play_media")


async def get_tts_duration(hass: HomeAssistant, tts_url: str) -> float:
    try:
        # Ensure we have the full URL
        if tts_url.startswith('/'):
            base_url = get_url(hass)
            full_url = f"{base_url}{tts_url}"
        else:
            full_url = tts_url

        # Use Home Assistant's aiohttp client session
        session = async_get_clientsession(hass)
        async with session.get(full_url) as response:
            if response.status != 200:
                _LOGGER.error(f"Failed to fetch TTS audio: HTTP {response.status}")
                return 0
            
            content = await response.read()

        # Use mutagen to get the duration
        audio = MP3(io.BytesIO(content))
        duration = audio.info.length
        
        return duration

    except Exception as e:
        _LOGGER.error(f"Error getting TTS duration: {e}")
        return 0


async def stream_run(hass: HomeAssistant, data: dict, stt_stream: Stream) -> None:
    stream_kwargs = data.get("stream", {})

    if "file" not in stream_kwargs:
        if url := data.get("stream_source"):
            stream_kwargs["file"] = url
        elif entity := data.get("camera_entity_id"):
            stream_kwargs["file"] = await get_stream_source(hass, entity)
        else:
            return

    stt_stream.open(**stream_kwargs)

    await hass.async_add_executor_job(stt_stream.run)


async def assist_run(
    hass: HomeAssistant,
    data: dict,
    context: Context = None,
    event_callback: PipelineEventCallback = None,
    stt_stream: Stream = None,
    conversation_id: str | None = None
) -> dict:
    _LOGGER.debug(f"assist_run called with conversation_id: {conversation_id}")
    
    # 1. Process assist_pipeline settings
    assist = data.get("assist", {})

    if pipeline_id := data.get("pipeline_id"):
        # get pipeline from pipeline ID
        pipeline = assist_pipeline.async_get_pipeline(hass, pipeline_id)
    elif pipeline_json := assist.get("pipeline"):
        # get pipeline from JSON
        pipeline = Pipeline.from_json(pipeline_json)
    else:
        # get default pipeline
        pipeline = assist_pipeline.async_get_pipeline(hass)

    if "start_stage" not in assist:
        # auto select start stage
        if pipeline.wake_word_entity:
            assist["start_stage"] = PipelineStage.WAKE_WORD
        elif pipeline.stt_engine:
            assist["start_stage"] = PipelineStage.STT
        else:
            raise Exception("Unknown start_stage")

    if "end_stage" not in assist:
        # auto select end stage
        if pipeline.tts_engine:
            assist["end_stage"] = PipelineStage.TTS
        else:
            assist["end_stage"] = PipelineStage.INTENT

    player_entity_id = data.get("player_entity_id")

    # 2. Setup Pipeline Run
    events = {}
    pipeline_run = None  # Define pipeline_run before the internal_event_callback
    tts_duration = 0

    def internal_event_callback(event: PipelineEvent):
        nonlocal pipeline_run
        _LOGGER.debug(f"Event: {event.type}, Data: {event.data}")

        events[event.type] = (
            {"data": event.data, "timestamp": event.timestamp}
            if event.data
            else {"timestamp": event.timestamp}
        )

        if event.type == PipelineEventType.STT_START:
            if player_entity_id and (media_id := data.get("stt_start_media")):
                play_media(hass, player_entity_id, media_id, "music")
        elif event.type == PipelineEventType.STT_END:
            stt_text = event.data.get("stt_output", {}).get("text", "").lower()
            # Check if the entire phrase matches any cancellation phrase
            if re.match(r'^(' + '|'.join(CANCELLATION_PHRASES) + r')$', stt_text.strip()):
                _LOGGER.info(f"Cancellation phrase detected: {stt_text}")
                if player_entity_id and (media_id := data.get("cancellation_media")):
                    play_media(hass, player_entity_id, media_id, "music")
                # Cancel the pipeline
                pipeline_run.stop(PipelineStage.STT)
            elif player_entity_id and (media_id := data.get("stt_end_media")):
                play_media(hass, player_entity_id, media_id, "music")
        elif event.type == PipelineEventType.ERROR:
            if event.data.get("code") == "stt-no-text-recognized":
                if player_entity_id and (media_id := data.get("stt_error_media")):
                    play_media(hass, player_entity_id, media_id, "music")
        elif event.type == PipelineEventType.TTS_END:
            if player_entity_id:
                tts = event.data["tts_output"]
                tts_url = tts["url"]

                async def simulate_wake_word_and_continue():
                    duration = await get_tts_duration(hass, tts_url)
                    events[PipelineEventType.TTS_END]["data"]["tts_duration"] = duration
                    _LOGGER.debug(f"Stored TTS duration: {duration} seconds")
                
                    # Set a timer to simulate wake word detection after TTS playback
                    await asyncio.sleep(duration)
                    await asyncio.sleep(1)  # Additional small delay
                
                    _LOGGER.debug("Simulating wake word detection after TTS playback")
                
                    # Get the proper wake word for this pipeline
                    wake_word_id = pipeline_run.pipeline.wake_word_id
                    wake_word_phrase = re.sub(r'_', ' ',wake_word_id)
                    wake_word_entity = pipeline_run.pipeline.wake_word_entity
                
                    # If wake_word_id is not set, try to get it from the entity
                    if not wake_word_id and wake_word_entity:
                        wake_word_entity_state = hass.states.get(wake_word_entity)
                        if wake_word_entity_state:
                            wake_word_id = wake_word_entity_state.attributes.get("wake_word_id")
                
                    # Default to "default" if we couldn't find a wake word ID
                    wake_word_id = wake_word_id or "default"
                    
                    # Simulate wake word detection end event
                    wake_word_event = PipelineEvent(
                        PipelineEventType.WAKE_WORD_END,
                        {"wake_word_output": {
                            "wake_word_id": wake_word_id,
                            "wake_word_phrase": wake_word_phrase,
                            "timestamp": time.time()
                        }}
                    )
                    pipeline_run.process_event(wake_word_event)

                # Schedule an async task to simulate wake word and continue pipeline
                hass.create_task(simulate_wake_word_and_continue())
                play_media(hass, player_entity_id, tts["url"], tts["mime_type"])

        if event_callback:
            event_callback(event)

    pipeline_run = PipelineRun(
        hass,
        context=context,
        pipeline=pipeline,
        start_stage=assist["start_stage"],
        end_stage=assist["end_stage"],
        event_callback=internal_event_callback,
        tts_audio_output=assist.get("tts_audio_output"),
        wake_word_settings=new(WakeWordSettings, assist.get("wake_word_settings")),
        audio_settings=new(AudioSettings, assist.get("audio_settings")),
    )

    # 3. Setup Pipeline Input
    pipeline_input = PipelineInput(
        run=pipeline_run,
        stt_metadata=stt.SpeechMetadata(
            language="",  # set in async_pipeline_from_audio_stream
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        ),
        stt_stream=stt_stream,
        intent_input=assist.get("intent_input"),
        tts_input=assist.get("tts_input"),
        conversation_id=conversation_id,  # Pass the conversation_id
        device_id=assist.get("device_id"),
    )

    try:
        # 4. Validate Pipeline
        await pipeline_input.validate()

        # 5. Run Stream (optional)
        if stt_stream:
            stt_stream.start()

        # 6. Run Pipeline
        await pipeline_input.execute()

        # Extract conversation_id from the INTENT_END event
        result_conversation_id = None
        if PipelineEventType.INTENT_END in events:
            intent_output = events[PipelineEventType.INTENT_END].get('data', {}).get('intent_output', {})
            result_conversation_id = intent_output.get('conversation_id')

        return {
            "events": events, 
            "conversation_id": result_conversation_id,
            "tts_duration": tts_duration
        }

    except AttributeError:
        pass  # 'PipelineRun' object has no attribute 'stt_provider'
    finally:
        if stt_stream:
            stt_stream.stop()

    # If we reach here due to an exception, return a default dictionary
    return {"events": events, "conversation_id": None, "tts_duration": 0}
    

def run_forever(
    hass: HomeAssistant,
    data: dict,
    context: Context,
    event_callback: PipelineEventCallback,
) -> Callable:
    _LOGGER.debug("Entering run_forever function")
    stt_stream = Stream()

    async def run_stream():
        while not stt_stream.closed:
            try:
                await stream_run(hass, data, stt_stream=stt_stream)
            except Exception as e:
                _LOGGER.debug(f"run_stream error {type(e)}: {e}")
            await asyncio.sleep(30)

    async def run_assist():
        conversation_id = None
        last_interaction_time = None
        while not stt_stream.closed:
            try:
                current_time = time.time()
                if last_interaction_time and current_time - last_interaction_time > 300:
                    conversation_id = None

                result = await assist_run(
                    hass,
                    data,
                    context=context,
                    event_callback=event_callback,
                    stt_stream=stt_stream,
                    conversation_id=conversation_id
                )
                new_conversation_id = result.get("conversation_id")
                if new_conversation_id:
                    conversation_id = new_conversation_id
                    last_interaction_time = current_time
                _LOGGER.debug(f"Conversation ID: {conversation_id}")
            except Exception as e:
                _LOGGER.exception(f"run_assist error: {e}")

    # Create coroutines
    run_stream_coro = run_stream()
    run_assist_coro = run_assist()

    # Schedule the coroutines as background tasks
    hass.loop.create_task(run_stream_coro, name="stream_assist_cc_run_stream")
    hass.loop.create_task(run_assist_coro, name="stream_assist_cc_run_assist")

    return stt_stream.close
