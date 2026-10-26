export const meta = {
  name: 'data-walk',
  description: 'Name a batch of anonymous DATA elements (ROM tables / RAM vars): propose from the referencing named subs -> independent verify at the bytes and the lower layer. Returns verified survivors for the title\'s apply tool to write.',
  phases: [
    { title: 'Propose', detail: 'one agent per data addr reads its referencing named subs -> proposes what the data IS + a name' },
    { title: 'Verify', detail: 'one independent agent per proposal drops to the raw bytes + the referencing code to confirm/amend/refute' },
  ],
}

// The method is here; the title's facts come in `context` (the title's walk-context.json; PHASES.md
// section 5). Moved from the vault skill /data-walk 2026-10-26. This is the ANALYTICAL lane only:
// strings (the oracle lane) and provenance names are deterministic and never reach it.
// args:
//   region  : "rom" | "ram"
//   targets : [{addr, refsubs?, preview?, rw?}]   from the title's frontier (with the named referencers)
//   seeds   : string?                             field maps, the region's known structure
//   context : {title, readable, lower, marker, refs_rom, refs_ram, rederive?, caveats?}
//             templates: {bb} = 'NN' (the referencer's bank is unknown here), {addr} = 4-hex address
// Returns { region, verified:[{addr,final_name,verdict,confidence,summary,evidence}] }.

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const C = A.context || {}
for (const k of ['title', 'readable', 'lower', 'refs_rom', 'refs_ram']) if (!C[k]) throw new Error(`context.${k} is required (the title's walk-context.json)`)
const T = (s, addr = '<ADDR>') => String(s).replaceAll('{bb}', 'NN').replaceAll('{addr}', addr)
const marker = C.marker || "the sub's block"
const region = (A.region || 'rom').toLowerCase()
const targets = A.targets || []
const seeds = A.seeds ? `\n\nKNOWN CONTEXT / region structure (anchors):\n${A.seeds}` : ''
const isRom = region === 'rom'
const refs = (addr) => T(isRom ? C.refs_rom : C.refs_ram, addr)

const METHOD = `
METHOD (do not re-trip):
- A data element's meaning is inferred from the NAMED sub(s) that reference it: a 'play_growth_animation' sub that reads blob_X => blob_X is the growth animation. Name from the referencer, not from squinting at bytes.
- VAGUE-BUT-CORRECT BEATS SPECIFIC-BUT-WRONG. If the exact semantics aren't earned, a domain+pattern name (<refsub>_data_<addr>, "..._rate_table") with the shape in the COMMENT is the right answer, not a confident guess. Data ALWAYS has an honest provenance name, so REFUTED -> <refsub>_data_<addr>, never an invented purpose.
- Strings are an ORACLE (the decoded text IS the bytes) and are named deterministically; you only see the analytical residue.`
const CAVEATS = METHOD + (C.caveats ? `\nTITLE CAVEATS (do not re-trip):\n${C.caveats}` : '')

const PROPOSE_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['proposals'],
  properties: {
    proposals: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['addr', 'name', 'evidence', 'confidence'],
        properties: {
          addr: { type: 'string', description: '4-hex address, no $' },
          name: { type: 'string', description: 'snake_case name for the DATA: what it holds (noun). Fall back to <refsub>_data_<addr> if the role is not earned.' },
          evidence: { type: 'string', description: 'which named sub references it and HOW (index expr, field, count), + the byte/value shape' },
          confidence: { enum: ['HIGH', 'MED', 'LOW'] },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['addr', 'final_name', 'verdict', 'confidence', 'summary', 'evidence'],
  properties: {
    addr: { type: 'string' },
    final_name: { type: 'string', description: 'the name to write; on REFUTED use <refsub>_data_<addr> (the honest provenance name)' },
    verdict: { enum: ['CONFIRMED', 'AMENDED', 'REFUTED'] },
    confidence: { enum: ['HIGH', 'MED', 'LOW'] },
    summary: { type: 'string', description: 'ONE line (<=220 chars), the CONCLUSION: what the data holds + its shape (length/stride/encoding) + which sub uses it. Written verbatim as the symbol comment.' },
    evidence: { type: 'string', description: 'the full basis: the referencing code / index math + the raw bytes / value range (kept in the run log)' },
  },
}

const proposePrompt = (t) => `You are STAGE 2 (symbol-table) of the DATA-WALK for ${C.title}. Propose a name for the ${isRom ? 'ROM data element (table/blob)' : 'RAM variable'} at $${t.addr} by reading the NAMED sub(s) that reference it.

Project root is the cwd.
Provenance (named referencers): ${t.refsubs || '(run the xref tool yourself)'}
Shape hint: ${t.preview || '(unknown)'}${t.rw ? `   access: ${t.rw}` : ''}

For $${t.addr}:
1. Find its referencers: ${refs(t.addr)}   (grouped by NAMED sub: this is the provenance).
2. Read those subs in the readable layer (${T(C.readable)}): HOW is $${t.addr} used? The index expression (idx*stride, a per-entity table), the field it feeds, the loop count, whether it is a rate / coord / palette / flag.
3. ${isRom
    ? `Inspect the bytes: note the length (distance to the next labelled address) + value range + encoding (text? LE pointers? small ints?).`
    : `Note where it is WRITTEN (that sets its meaning) and where READ (its consumers). A per-turn value, a cursor, a bitfield, a resource triple?`}
4. Propose a NOUN name for what it HOLDS (not what it is near). HIGH only if the referencer makes the role unambiguous; else MED with a domain+pattern name; LOW falls back to <refsub>_data_$${t.addr}. Do NOT inflate: the verifier arbitrates.
${seeds}
${CAVEATS}

Return ONLY the structured proposals. Your output IS data for the verifier, not prose.`

const verifyPrompt = (p) => `You are STAGE 3 (the independent verifier / typecheck) for the DATA-WALK of ${C.title}. A PROPOSER named $${p.addr} "${p.name}" (confidence ${p.confidence}). INDEPENDENTLY confirm, amend, or refute by dropping to a LOWER altitude than the proposer: the RAW BYTES + the referencing code in ${T(C.lower)} (ground truth; the readable layer is lossy). The name must be earned by how the data is actually used.

Project root is the cwd. Do NOT trust the proposer's reading. Instead:
1. Re-derive the referencers yourself: ${refs(p.addr)}
2. Read the referencing sub in ${T(C.lower)} (find ${marker}): confirm the index math / access pattern the name claims. ${isRom ? 'Confirm the table length (where does it end? the next labelled address) and the value encoding from the raw bytes.' : 'Confirm WRITE vs READ direction and what value it is assigned.'}
${C.rederive ? `3. Optional cross-check: ${T(C.rederive)} for the referencing sub.` : ''}

If a HIGH claim maps to a known struct/formula, that is a deterministic ORACLE match: say so, highest confidence. A strong extra check for a claimed coordinate/index table: cross-reference an independent ground truth (adjacency, known counts) and report whether it agrees.

Verdicts:
- CONFIRMED: bytes + referencing code support the name as-is.
- AMENDED: real role but wrong/imprecise name -> give the correct final_name.
- REFUTED: role not earned -> final_name = "<refsub>_data_${p.addr.toLowerCase()}" (data ALWAYS has a provenance name).
Return a one-line 'summary' (the CONCLUSION): written verbatim as the symbol comment.
Proposer evidence (reference, NOT authority): ${p.evidence}
${seeds}
${CAVEATS}

Return ONLY the structured verdict.`

phase('Propose')
const perTarget = await pipeline(
  targets,
  (t) => agent(proposePrompt(t), { label: `propose:${t.addr}`, phase: 'Propose', schema: PROPOSE_SCHEMA }),
  (res) => parallel(((res && res.proposals) || []).map(p => () =>
    agent(verifyPrompt(p), { label: `verify:${p.addr}`, phase: 'Verify', schema: VERDICT_SCHEMA }))),
)

const verified = perTarget.flat().filter(Boolean)
const tally = verified.reduce((m, v) => (m[v.verdict] = (m[v.verdict] || 0) + 1, m), {})
log(`data-walk ${region}: ${verified.length} verified (${JSON.stringify(tally)})`)
return { region, verified }
