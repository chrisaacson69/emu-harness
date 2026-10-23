"""Walk: the deterministic half of the naming walks (label / var / data) -- batch selection and the
regen guard. The agents propose and verify; this picks what they see and refuses a write that
changes more than names. A walk knows no emulator."""
