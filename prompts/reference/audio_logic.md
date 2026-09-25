Evaluate only `Audio_Logic` using the supplied audiovisual clip. Use visible
events only to determine whether the audible design is synchronized,
motivated, spatially coherent, and appropriate to the scene; do not let visual
quality or narrative relevance substitute for audio evidence.

First determine whether an actual audible signal is present. The clip may
contain a silent audio track. If no sound is actually audible, return
`evidence_status` as `not_observable` and both scores as 0.0, regardless of
visible actions that would normally make sounds.

Observable evidence: dialogue presence and delivery as sound, ambience,
foley, impacts, movement sounds, music, silence, layering, dynamics, rhythm,
spatialization, audiovisual synchronization, and sonic transitions. Distinguish
sounds that are directly audible from visual events that merely suggest a
possible sound. Never claim a sound exists unless it is audible.

Alignment asks whether the audible design locally matches the Skill's stated
sound, music, rhythm, or silence logic. Inspirational Value asks whether the
audio provides a specific, transferable sonic idea that could enrich the Skill
even when it does not align closely.

If the waveform is silent or contains no usable evidence, say so and score
accordingly. Never infer sound from the Skill text.

Skill Text:
{skill_text}
