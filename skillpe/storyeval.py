import re

DESCRIPTION_PROMPT = 'Please describe the full video in detail, in temporal order. The video may be generated, so be explicit when an action, object, or subject is unclear or visually ambiguous.'

def scoring_prompt(description: str, original_prompt: str, event_list: list[str]) -> str:
    events = "\n".join(f"{idx + 1}. {event}" for idx, event in enumerate(event_list))
    return (
        "You are evaluating a generated text-to-video result using the StoryEval protocol.\n\n"
        f"Video description from the previous step:\n{description}\n\n"
        f"Original prompt:\n{original_prompt}\n\n"
        f"The prompt contains {len(event_list)} ordered events:\n{events}\n\n"
        "For each event, judge strictly whether it is visibly completed in the video. "
        "If an event is blurry, vague, only implied, or hard to identify, mark it as 0. "
        "If later events require the same subject or object as earlier events but the video changes it, "
        "mark the inconsistent later event as 0. Explain briefly, then end with exactly one line:\n"
        "Finally we have [COMPLETE_LIST]: x, y, z\n"
        "where each value is 1 for completed or 0 for not completed, and the list length must match the event count."
    )
