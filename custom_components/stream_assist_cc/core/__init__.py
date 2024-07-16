import asyncio
import aiohttp
import os
import tempfile
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

from .stream import Stream

_LOGGER = logging.getLogger(__name__)

DOMAIN = "stream_assist_cc"
EVENTS = ["wake", "stt", "intent", "tts"]
CANCELLATION_PHRASES = [
    r'\bnevermind\b', r'\bnever mind\b', r'\bthank you\b', r'\bcancel that\b', 
    r'\bcancel\b', r'\babort\b', r'\bquit\b', r'\bexit\b', r'\bend\b', r'\bforget it\b', 
    r'\bthat\'s all\b', r'\bthat is all\b'
]

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

async def get_audio_length(file_path: str) -> float:
    if file_path.startswith(('http://', 'https://')):
        async with aiohttp.ClientSession() as session:
            async with session.get(file_path) as response:
                if response.status == 200:
                    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as temp_file:
                        temp_file.write(await response.read())
                        temp_file_path = temp_file.name
                    try:
                        audio = MP3(temp_file_path)
                        return audio.info.length
                    finally:
                        os.unlink(temp_file_path)
    else:
        audio = MP3(file_path)
        return audio.info.length
    return 0
    
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
        nonlocal tts_duration
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
                # Schedule the audio length calculation
                hass.async_create_task(calculate_tts_duration(tts_url))
                play_media(hass, player_entity_id, tts["url"], tts["mime_type"])

        if event_callback:
            hass.async_create_task(event_callback(event))
    async def calculate_tts_duration(tts_url):
        nonlocal tts_duration
        tts_duration = await get_audio_length(tts_url)
        _LOGGER.debug(f"TTS duration: {tts_duration} seconds")
        
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
        device_id=data.get("device_id"),  # Pass the device_id if available
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

        return {"events": events, "conversation_id": result_conversation_id}
        # Wait for TTS playback to complete
        if tts_duration > 0:
            _LOGGER.debug(f"Waiting for {tts_duration} seconds before next interaction")
            await asyncio.sleep(tts_duration)

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


def play_media(hass: HomeAssistant, entity_id: str, media_id: str, media_type: str):
    service_data = {
        "entity_id": entity_id,
        "media_content_id": media_player.async_process_play_media_url(hass, media_id),
        "media_content_type": media_type,
    }

    coro = hass.services.async_call("media_player", "play_media", service_data)
    hass.async_create_task(coro, "stream_assist_cc_play_media")

def run_forever(
    hass: HomeAssistant,
    data: dict,
    context: Context,
    event_callback: PipelineEventCallback,
) -> Callable:
    _LOGGER.debug("Entering run_forever function")
    stt_stream = Stream()

    async def run_stream():
        _LOGGER.debug("Entering run_stream coroutine")
        while not stt_stream.closed:
            try:
                _LOGGER.debug("Attempting to run stream")
                await stream_run(hass, data, stt_stream=stt_stream)
                _LOGGER.debug("Stream run completed")
            except Exception as e:
                _LOGGER.error(f"run_stream error {type(e)}: {e}")
            _LOGGER.debug("Waiting 30 seconds before next stream run")
            await asyncio.sleep(30)

async def run_assist():
    _LOGGER.debug("Entering run_assist coroutine")
    conversation_id = None
    last_interaction_time = None
    while not stt_stream.closed:
        try:
            _LOGGER.debug("Starting assist run")
            current_time = time.time()
            if last_interaction_time and current_time - last_interaction_time > 300:
                _LOGGER.debug("Resetting conversation ID due to inactivity")
                conversation_id = None

            result = await assist_run(
                hass,
                data,
                context=context,
                event_callback=event_callback,
                stt_stream=stt_stream,
                conversation_id=conversation_id
            )
            _LOGGER.debug(f"Assist run completed. Result: {result}")
            new_conversation_id = result.get("conversation_id")
            tts_duration = result.get("tts_duration", 0)
            
            if new_conversation_id:
                conversation_id = new_conversation_id
                last_interaction_time = current_time
            _LOGGER.debug(f"Updated Conversation ID: {conversation_id}")

            # Wait for a short period after TTS playback before next run
            await asyncio.sleep(max(1, tts_duration + 1))
        except Exception as e:
            _LOGGER.exception(f"run_assist error: {e}")
            _LOGGER.debug("Waiting 1 second before next assist run")
            await asyncio.sleep(1)

    _LOGGER.debug("Creating coroutines")
    run_stream_coro = run_stream()
    run_assist_coro = run_assist()

    _LOGGER.debug("Scheduling coroutines as background tasks")
    hass.loop.create_task(run_stream_coro, name="stream_assist_cc_run_stream")
    hass.loop.create_task(run_assist_coro, name="stream_assist_cc_run_assist")

    _LOGGER.debug("run_forever function completed, returning close function")
    return stt_stream.close

def new(cls, kwargs: dict):
    if not kwargs:
        return cls()
    kwargs = {k: v for k, v in kwargs.items() if hasattr(cls, k)}
    return cls(**kwargs)
