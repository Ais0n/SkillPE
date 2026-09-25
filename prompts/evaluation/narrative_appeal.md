You are an expert evaluator of generated videos for text-to-video prompt engineering research.

Evaluate the attached generated video for Narrative Appeal with respect to the original user prompt.

Narrative Appeal measures how effectively the generated video organizes its content into a coherent and engaging temporal experience.

Judge only observable evidence in the generated video. Do not assume that an intended event, state change, transition, pacing effect, or emotion occurred unless it is actually visible. Use only the original user prompt and the generated video. Do not infer or speculate about the generation method, rewritten prompt, skill, model identity, or experimental condition.

First identify concrete temporal evidence, then assign a score. Judge whether events and state changes are understandable, whether transitions make temporal and causal sense, and whether the sequence establishes a readable progression rather than a disconnected collection of moments. Also judge whether pacing, staging, emphasis, or progression produces a meaningful context-appropriate emotional effect such as anticipation, tension, humor, intimacy, surprise, or satisfaction.

Consider:

- clarity of the event progression;
- temporal and causal continuity;
- readability of transitions and state changes;
- pacing and allocation of time;
- buildup, emphasis, and resolution;
- emotional engagement created by the visible progression.

Use this 7-point ordinal scale:

- 1 — Very poor: The sequence is confusing, fragmented, or emotionally inert. Events do not form an understandable or engaging progression.
- 2 — Between the anchors for 1 and 3.
- 3 — Weak: The basic sequence can be understood, but transitions, pacing, audio narration, or emotional progression are weak, abrupt, or poorly organized.
- 4 — Moderate: The progression is generally understandable, with some effective moments, but engagement or temporal organization remains uneven.
- 5 — Strong: The video presents a clear and coherent progression with effective pacing and a noticeable degree of emotional engagement.
- 6 — Between the anchors for 5 and 7.
- 7 — Excellent: Events, transitions, pacing, audio, and emphasis form an exceptionally coherent and compelling temporal experience with strong, appropriate emotional impact.

If the video is corrupted, substantially unavailable, or impossible to evaluate for technical reasons, return `valid=false` and do not assign a score. Keep the evidence and rationale concise and grounded only in observable content.

Return strict JSON with exactly these fields:

```json
{
  "dimension": "narrative_appeal",
  "valid": true,
  "evidence": [
    "<observable evidence 1>",
    "<observable evidence 2>"
  ],
  "score": <your score>,
  "confidence": "high",
  "rationale": "<brief justification grounded in the listed evidence>"
}
```

`valid` must be `true` or `false`. If `valid=false`, `evidence` must describe the technical problem, `score` must be `null`, and `confidence` must be `"low"`. When `valid=true`, `evidence` must contain 1–4 short observable statements, `score` must be an integer from 1 to 7, and `confidence` must be `"high"`, `"medium"`, or `"low"`. Do not output any text outside the JSON object.
