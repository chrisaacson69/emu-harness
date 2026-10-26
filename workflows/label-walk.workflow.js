export const meta = {
  name: 'label-walk',
  description: 'Name a batch of anonymous subs: propose from the readable layer -> independent verify at the lower layer. Returns verified survivors for the title\'s apply tool to write.',
  phases: [
    { title: 'Propose', detail: 'one agent per cluster reads the readable layer + call graph -> proposes functional names' },
    { title: 'Verify', detail: 'one independent agent per proposal drops to the lower layer to confirm/amend/refute' },
  ],
}

// The method is here; the title's facts come in `context` (the title's walk-context.json, merged into
// the args by the coordinator; PHASES.md section 5). Moved from the vault skill /label-walk 2026-10-26.
// args:
//   bank     : int            the code bank
//   clusters : [{name,addrs}]  a batch from the title's prep tool (leaves first; regen between batches)
//   seeds    : string?         already-named callees / context
//   oracle   : string?         known formulas / structures for the deterministic-match lane
//   context  : {title, readable, lower, marker, callers, calls, rederive?, caveats?}
//              readable / lower / callers / calls / rederive are templates: {bb} = 2-digit bank, {addr} = 4-hex address
// Returns { bank, verified:[{addr,final_name,verdict,confidence,summary,evidence}] }.
// The workflow NEVER writes files: the title's apply tool is the single writer and runs the guard.

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const C = A.context || {}
for (const k of ['title', 'readable', 'lower', 'callers', 'calls']) if (!C[k]) throw new Error(`context.${k} is required (the title's walk-context.json)`)
const bank = A.bank
const bb = String(bank).padStart(2, '0')
const T = (s, addr = '<ADDR>') => String(s).replaceAll('{bb}', bb).replaceAll('{addr}', addr)
const marker = C.marker || "the sub's block"
const clusters = A.clusters || []
const seeds = A.seeds ? `\n\nKNOWN SEEDS / already-named callees (use as anchors; leaves-first means many callees are named):\n${A.seeds}` : ''
const oracle = A.oracle ? `\n\nORACLE HINTS (if a sub's behavior maps to one of these known formulas/structs, say so — that is a deterministic match, highest confidence):\n${A.oracle}` : ''
const CAVEATS = C.caveats ? `\nTITLE CAVEATS (do not re-trip):\n${C.caveats}` : ''

const PROPOSE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['proposals'],
  properties: {
    proposals: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['addr', 'name', 'evidence', 'confidence'],
        properties: {
          addr: { type: 'string', description: '4-hex address, no $ (e.g. 8003)' },
          name: { type: 'string', description: 'snake_case functional name (verb_noun); what it DOES, not what it is near' },
          evidence: { type: 'string', description: 'fields touched / calls / callers: the basis for the name' },
          confidence: { enum: ['HIGH', 'MED', 'LOW'] },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['addr', 'final_name', 'verdict', 'confidence', 'summary', 'evidence'],
  properties: {
    addr: { type: 'string' },
    final_name: { type: 'string', description: 'the name to write; on REFUTED use helper_<addr>' },
    verdict: { enum: ['CONFIRMED', 'AMENDED', 'REFUTED'] },
    confidence: { enum: ['HIGH', 'MED', 'LOW'] },
    summary: { type: 'string', description: 'ONE line (<=220 chars), the CONCLUSION not the trace: what the sub does + the key fields/formula. Written verbatim as the symbol comment, so a clean human description with no preamble.' },
    evidence: { type: 'string', description: 'the full lower-layer basis (kept in the run log, not the symbol table)' },
  },
}

const proposePrompt = (cluster) => `You are STAGE 2 (symbol-table) of a multi-pass decompiler labeling bank_${bb} of ${C.title}. Propose functional names for these anonymous subs by READING THE READABLE LAYER.

Project root is the cwd. Subs to name (addresses): ${cluster.addrs.map(a => '$' + a).join(', ')}

For EACH address:
1. Read its body in ${T(C.readable)}.
2. Run: ${T(C.callers)}   and   ${T(C.calls)}
   (who calls it = its purpose; what it calls = named callees give it meaning).
3. Propose a snake_case verb_noun name describing what it DOES. NOT what it sits near. Cite the fields it touches, the calls, and the callers as evidence. Confidence HIGH only if the body + call graph make the function unambiguous; MED if plausible; LOW if a structural guess (the verifier arbitrates, so don't inflate).
${seeds}${oracle}
${CAVEATS}

Return ONLY the structured proposals. Your output IS data for the verifier, not prose for a human.`

const verifyPrompt = (p) => `You are STAGE 3 (the independent verifier / typecheck) for bank_${bb} of ${C.title}. A PROPOSER named sub $${p.addr} "${p.name}" (confidence ${p.confidence}) from the readable layer. INDEPENDENTLY confirm, amend, or refute it by dropping to a LOWER altitude than the proposer used — ${T(C.lower, p.addr)}, which is ground truth (the readable layer is lossy). Be skeptical; the name must be earned.

Project root is the cwd. Do NOT trust what the proposer read. Instead:
1. Read the sub in ${T(C.lower, p.addr)}: find ${marker} for $${p.addr} and read its instructions.
2. Cross-check the call graph independently: ${T(C.callers, p.addr)}  and  ${T(C.calls, p.addr)}
${C.rederive ? `3. Optionally re-derive the readable form: ${T(C.rederive, p.addr)}` : ''}

Verdicts:
- CONFIRMED: the lower layer supports the proposed name as-is.
- AMENDED: the function is real but the name is wrong/imprecise -> give the correct final_name.
- REFUTED: not enough evidence to name by purpose -> final_name = "helper_${p.addr}".
Also return a one-line 'summary' (the CONCLUSION, not the trace): it is written verbatim as the symbol comment.
Proposer evidence (for reference, NOT authority): ${p.evidence}
${seeds}
${CAVEATS}

Return ONLY the structured verdict.`

phase('Propose')
const perCluster = await pipeline(
  clusters,
  (cluster) => agent(proposePrompt(cluster), { label: `propose:${cluster.name}`, phase: 'Propose', schema: PROPOSE_SCHEMA }),
  (res, cluster) => parallel(((res && res.proposals) || []).map(p => () =>
    agent(verifyPrompt(p), { label: `verify:${p.addr}`, phase: 'Verify', schema: VERDICT_SCHEMA }))),
)

const verified = perCluster.flat().filter(Boolean)
const tally = verified.reduce((m, v) => (m[v.verdict] = (m[v.verdict] || 0) + 1, m), {})
log(`bank_${bb}: ${verified.length} verified (${JSON.stringify(tally)})`)
return { bank, verified }
