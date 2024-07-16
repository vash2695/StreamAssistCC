# Stream Assist CC

# NOTE: I make changes to this frequently that may break things! Use at your own risk.

This fork is an attempt at implementing continued conversation and other features within Stream Assist.

Features added so far:
* Continued conversation: conversation_id is preserved between interactions for a given entry with a timeout of 300 seconds, additional testing needed to verify timeout reliability
* STT End media: Now both the start and end of the STT phase have options for audio feedback. Also added a field for this in the config options
* Audio feedback for "no-text-recognized" events
* More initial config options: All available options are now in the initial config screen

Future goals:
* Wake Word skip: Add some way to skip the wake word phase on follow up interactions
   * Could be implemented with a function call or just enabled for all interactions with a toggle in config
   * Must also skip intent with closing phrases like Thank You, Nevermind, That is all, Stop, etc. so the LLM doesn't get prompted needlessly
   * *Current roadblock is finding a way to make the integration aware of exactly when TTS playback has ended for the most recent interaction. Without this, the STT phase would initiate before TTS playback ends, so the user would not have the opportunity to make a request in time.*
* Globally continued conversations: Add the option to pass conversation IDs across all integration entries
   * This would require the integration to also provide updated area and device information
* Expose more of the integration to automations
  * Example: A service call that allows you to select an integration entry and trigger the assistant pipeline at the intent phase with predefined data

