Prompt:
{prompt}

Rate narrative_complexity with a continuous float score in [0.0, 1.0]:
the complexity of the sequence of events and its causal/temporal structure,
not merely the detail in a static scene. This score alone determines the
complexity stratum; do not average it with other quality attributes.

Also assign exactly one primary_category:
- human_centric
- creature
- environment
- object_focus
- abstract_creative

Return exactly this JSON schema:
{{
  "narrative_complexity": 0.0,
  "primary_category": "human_centric"
}}
