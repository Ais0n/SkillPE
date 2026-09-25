You are an expert in prompt engineering for video generation. Based on the provided movie-clip examples, your task is to add practical usage guidance to an existing seed skill.

This round includes only two types of examples:
- `positive`: Positive examples for which the skill is appropriate. Use them to learn the core shot structure, narrative relationships, and cinematic expression that the skill should reinforce.
- `negative`: Hard negatives that appear superficially similar but for which the skill should not be used. Use them to learn the boundaries where the skill must not be forced onto a prompt.

Important constraints:
- Do not use near-miss reasoning. This round does not learn adjacent expansion or pruning heuristics.
- The goal is not to rewrite the skill's shot template. The goal is to summarize the experience that a downstream agent should follow when selecting and applying the skill.
- Positive examples answer "what should be added and when it should be strengthened." Negative examples answer "what should not be added and when the skill should not be selected."

Read the seed skill and the batch of positive and negative examples, then return valid JSON with the following fields:
{
  "batch_summary": "Summarize in 2-4 sentences what this batch reveals about the skill's usage and boundaries.",
  "core_invariants": ["The core narrative and shot-organization invariants that this skill must preserve."],
  "cinematic_patterns": ["Transferable cinematic techniques from the positive examples, such as subject blocking, spatial organization, shot transitions, and changes in shot scale."],
  "modifiable_dimensions": ["Dimensions that may be adjusted for different queries."],
  "strengthen_when": ["Query patterns for which specific parts of the skill should be strengthened."],
  "weaken_when": ["Query patterns for which specific parts of the skill should be weakened."],
  "do_not_force_when": ["Queries for which this skill should not be forced."],
  "rhythm_and_cut_notes": ["How to arrange cutting rhythm, action synchronization points, audiovisual rhythm, and the magnitude of camera movement."],
  "application_notes": ["Concise usage guidance for the downstream agent."]
}

Requirements:
- Summarize only transferable experience. Do not repeat specific movie titles, character names, or overly detailed plot points.
- Every recommendation must support skill selection and skill application, not expand the material into a new narrative template.
- Derive `core_invariants`, `cinematic_patterns`, and `strengthen_when` primarily from the positive examples.
- `do_not_force_when` must fully incorporate the boundary information from the negative examples.
- Do not output Markdown or any additional explanation.
