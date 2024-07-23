# Stream Assist CC

# NOTE: I make changes to this frequently that may break things! Use at your own risk.

This fork is an attempt at implementing continued conversation and other features within Stream Assist.

Features added so far:
* Continued conversation: conversation_id is preserved between interactions for a given entry with a timeout of 300 seconds, though for now the timeout doesn't seem to work consistently
* Wake Word skip: The wake word phase can be automatically skipped on follow-up interactions
    * Still a work in progress, very buggy
* STT End media: Now both the start and end of the STT phase have options for audio feedback. Also added a field for this in the config options
* STT Error media: When there is an error in the STT phase (like no-text-recognized), a distinct error sound can be played
* More initial config options: All available options are now in the initial config screen
* Added cancel phrases like: "nevermind", "never mind", "thank you", "cancel that", "cancel",
    "abort", "quit", "exit", "end", "forget it", "that's all", "that is all" so that you can cancel the continuous conversation feature

Future goals:
* Globally continued conversations: Add the option to pass conversation IDs across all integration entries
    * This would require the integration to also provide updated area and device information
* Expose more of the integration to automations
    * Example: A service call that allows you to select an integration entry and trigger the assistant pipeline at the intent phase with predefined data
