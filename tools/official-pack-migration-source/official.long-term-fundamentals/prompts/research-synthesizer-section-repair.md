Repair only the report sections named by `repair_sections` in the task payload.

Return exactly one JSON object with this shape:

{"section_patches": {"<requested section>": <replacement value>}}

Do not regenerate any unrequested section, return `invalid_synthesis`,
or add explanatory prose. Existing valid sections are preserved by the caller and
must not be regenerated. Keep the report language requested by the task. Use
only supplied evidence and preserve existing evidence IDs, claim kinds, and
numeric values; never invent facts, citations, or numbers. If a requested
section cannot be supported, return a concise explicit unknown value rather
than fabricating content. The caller may accept a legacy response for
compatibility, but it will extract only the requested fields.
