Length-control protocol (identical for every method):
Write the pe_prompt in English for exactly the requested 10-second video.
The target length is {budget} whitespace-delimited words. The permitted range is {lower} through {upper} words inclusive, counted by splitting the pe_prompt on whitespace. JSON keys and surrounding JSON are not counted.
Meet this range without padding, repeated sentences, or adding new subjects, causal events, or an alternative ending. Preserve all original events and their order. Adjust the precision of staging, framing, light, timing, and physical or ambient sound to fit the budget. Do not claim that a longer budget means a longer video.
This word-budget requirement takes precedence over generic requests to be concise. Do not output a word-count annotation. Return only valid JSON with the key pe_prompt.
