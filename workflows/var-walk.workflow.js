export const meta = {
  name: 'var-walk',
  description: 'Name a batch of subs\' frame slots (arg1..arg4 / localN): propose by role from the readable layer -> independent verify at the lower layer + caller propagation. Returns per-slot verdicts for the title\'s apply tool to write.',
  phases: [
    { title: 'Propose', detail: 'one agent per sub reads the readable layer + callers -> proposes a role name for each positional slot' },
    { title: 'Verify', detail: 'one independent agent per sub drops to the lower layer + caller pushes to confirm/amend/refute each slot' },
  ],
}

// The method is here; the title's facts come in `context` (the title's walk-context.json; PHASES.md
// section 5). Moved from the vault skill /var-walk 2026-10-26. For titles whose decompiler prints frame
// slots positionally (the VM track).
// args:
//   bank    : int                          the code bank
//   subs    : [{addr, name, slots:[...]}]   a batch of NAMED subs with positional slots (the title's prep)
//   seeds   : string?                       role-inference vocabulary
//   single  : bool?                         ONE fused agent per sub (propose + self-verify): half the agents,
//                                           but no independent verifier; the coordinator must skim verdicts
//   context : {title, readable, lower, marker, callers, rederive?, frame_hint?, frame_map?, caveats?}
//             (frame_map: how the lower layer addresses each slot; packed-mode verifiers get it)
//             templates: {bb} = 2-digit bank, {addr} = 4-hex address
// Returns { bank, verified:[{addr, name, slots:[{slot,final_name,verdict,confidence,summary}]}] }.

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const C = A.context || {}
for (const k of ['title', 'readable', 'lower', 'callers']) if (!C[k]) throw new Error(`context.${k} is required (the title's walk-context.json)`)
const bank = A.bank
const bb = String(bank).padStart(2, '0')
const T = (s, addr = '<ADDR>') => String(s).replaceAll('{bb}', bb).replaceAll('{addr}', addr)
const marker = C.marker || "the sub's block"
const subs = A.subs || []
const seeds = A.seeds ? `\n\n${A.seeds}` : ''
const single = !!A.single
const frameHint = C.frame_hint ? ` — ${C.frame_hint}` : ''

const METHOD = `
METHOD (do not re-trip):
- A frame slot is named by its ROLE, not its number. The SAME slot number means different things in different subs.
- Name a slot ONLY if it has ONE coherent role across the whole sub. A slot REUSED for two disjoint roles stays POSITIONAL: that is the honest "this is scratch" signal, not a miss.`
const CAVEATS = METHOD + (C.caveats ? `\nTITLE CAVEATS (do not re-trip):\n${C.caveats}` : '')

const PROPOSE_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['addr', 'slots'],
  properties: {
    addr: { type: 'string', description: '4-hex stub address, no $' },
    slots: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false, required: ['slot', 'name', 'role', 'confidence'],
        properties: {
          slot: { type: 'string', description: 'the positional token being named: arg1..arg4 or localN' },
          name: { type: 'string', description: 'snake_case role name. Leave as the SAME positional token (e.g. "local10") if it is reused scratch with no single role.' },
          role: { type: 'string', description: 'the usage that justifies the name: fields dereferenced, helper args, caller-pushed expression, loop/accumulator pattern' },
          confidence: { enum: ['HIGH', 'MED', 'LOW'] },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['addr', 'name', 'slots'],
  properties: {
    addr: { type: 'string' },
    name: { type: 'string', description: 'the sub\'s function name (echo from input, for the section comment)' },
    slots: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false, required: ['slot', 'final_name', 'verdict', 'confidence', 'summary'],
        properties: {
          slot: { type: 'string' },
          final_name: { type: 'string', description: 'the name to write for this slot; on REFUTED this is ignored (slot stays positional)' },
          verdict: { enum: ['CONFIRMED', 'AMENDED', 'REFUTED'], description: 'REFUTED = leave positional (reused scratch / not enough evidence)' },
          confidence: { enum: ['HIGH', 'MED', 'LOW'] },
          summary: { type: 'string', description: 'ONE short line (<=140 chars): what this slot holds + the evidence. Becomes the symbol comment verbatim.' },
        },
      },
    },
  },
}

const proposePrompt = (s) => `You are STAGE 2 (symbol-table, variable side) of a multi-pass decompiler labeling bank_${bb} of ${C.title}. The sub $${s.addr} is "${s.name}", already understood. Propose a ROLE NAME for each of its positional frame slots by READING THE READABLE LAYER and its CALLERS.

Project root is the cwd. Slots to name in $${s.addr}: ${s.slots.join(', ')}

Steps:
1. Read the body in ${T(C.readable, s.addr)}: find '${s.name}(' and read it.
2. For each slot, infer its role from HOW it is used:
   - args: what fields/helpers consume it; AND run  ${T(C.callers, s.addr)}  then read those call sites to see the EXPRESSION each caller pushes into that arg (caller propagation: strong evidence).
   - locals: how it is computed and consumed across the whole body.
3. Give a snake_case role name (amount, gain, idx, rate, total, ...).
${seeds}
${CAVEATS}

Return ONLY the structured proposals. Your output IS data for the verifier.`

const verifyPrompt = (s, p) => `You are STAGE 3 (independent verifier / typecheck, variable side) for $${s.addr} "${s.name}" in bank_${bb} of ${C.title}. A PROPOSER named its frame slots from the readable layer. INDEPENDENTLY confirm/amend/refute EACH slot by dropping to a LOWER altitude, ${T(C.lower, s.addr)} (ground truth; the readable layer is lossy), and by checking caller propagation yourself. A slot name must be EARNED by its actual use.

Project root is the cwd. Do NOT trust the proposer's reading. Instead:
1. Read the sub in ${T(C.lower, s.addr)}: find ${marker} for $${s.addr} and read which frame offsets are loaded/stored/pushed, and into what${frameHint}.
2. Caller propagation: ${T(C.callers, s.addr)}: read the pushes at each call site. The expression a caller pushes into an arg slot is strong evidence of its role.
${C.rederive ? `3. Optionally re-derive the readable form: ${T(C.rederive, s.addr)}` : ''}

Per slot verdict:
- CONFIRMED: the lower layer + callers support the proposed name.
- AMENDED: real role, wrong/imprecise name -> give the correct final_name.
- REFUTED: reused scratch (two disjoint roles) OR not enough evidence -> the slot STAYS POSITIONAL (final_name ignored; say why in summary).
Each slot's 'summary' (<=140 chars) is written verbatim as the symbol comment.
Proposer's proposals (reference, NOT authority): ${JSON.stringify(p && p.slots || [])}
${seeds}
${CAVEATS}

Return ONLY the structured verdict.`

// PACKED mode: the title's prep attached each sub's packet (prep --pack): the material the agents
// above fetch with 2-4 tool calls, extracted deterministically. Each turn re-sends an agent's whole
// standing context (~50k tokens), so an agent costs about turns x 50k; with the packet in the prompt
// it needs one turn. The verifier's packet is the lower layer only (no C), so its independence is
// structural, not a request.
const packedProposePrompt = (s) => `You are STAGE 2 (symbol-table, variable side) of a multi-pass decompiler labeling bank_${bb} of ${C.title}. Sub $${s.addr} is "${s.name}", already understood. Propose a ROLE NAME for each positional frame slot: ${s.slots.join(', ')}.

EVERYTHING YOU NEED IS BELOW. Do not use tools, do not open files: answer from this packet. (It is extracted from the generated C, so it is the same text you would read.)

== The sub's C (the readable layer) ==
${s.packet.c}

== Every caller's call line (caller propagation: the expression pushed into each arg is strong evidence) ==
${s.packet.caller_c.join('\n')}

== Signatures of the subs it calls (their parameter names say what a pushed value is) ==
${s.packet.callees.join('\n') || '(none)'}

Name each slot by its ROLE from how it is used: fields dereferenced, helpers it feeds, the caller-pushed expression, loop / accumulator pattern. snake_case.
${seeds}
${CAVEATS}

Return ONLY the structured proposals. Your output IS data for the verifier.`

const packedVerifyPrompt = (s, p) => `You are STAGE 3 (independent verifier / typecheck, variable side) for $${s.addr} "${s.name}" in bank_${bb} of ${C.title}. A PROPOSER named its frame slots from the readable C. You do NOT get the C: you check at the LOWER layer, the VM bytecode, plus what each caller pushes. A slot name must be EARNED by its actual use there.

EVERYTHING YOU NEED IS BELOW. Do not use tools, do not open files: answer from this packet.

== The sub's VM asm (ground truth) ==
${s.packet.vm}

== The VM asm before each call to it (the pushes; the last line is the call) ==
${s.packet.caller_vm.join('\n\n')}

Read which frame slots are loaded / stored / pushed and into what${frameHint}.${C.frame_map ? `\n${C.frame_map}` : ''}

Per slot verdict:
- CONFIRMED: the VM asm and the caller pushes support the proposed name.
- AMENDED: real role, wrong/imprecise name -> give the correct final_name.
- REFUTED: reused scratch (two disjoint roles) OR not enough evidence -> the slot STAYS POSITIONAL (final_name ignored; say why).
Each slot's 'summary' (<=140 chars) is written verbatim as the symbol comment; cite the VM evidence (opcode, slot, address).
Proposer's names (reference, NOT authority): ${JSON.stringify(p && p.slots || [])}
${seeds}
${CAVEATS}

Return ONLY the structured verdict.`

const fusedPrompt = (s) => `You name the positional frame slots of sub $${s.addr} "${s.name}" in bank_${bb} of ${C.title}: you are BOTH proposer and verifier in one pass (token-economy mode). Give each slot a role name backed by lower-layer + caller evidence, not a guess.

Project root is the cwd. Slots to name in $${s.addr}: ${s.slots.join(', ')}

Steps (one tool pass each):
1. Read the body in ${T(C.readable, s.addr)}: find '${s.name}(' and read it.
2. Confirm against ${T(C.lower, s.addr)} where a slot's role is non-obvious (find ${marker}${frameHint}).
3. Caller propagation: ${T(C.callers, s.addr)}: the expression each caller pushes into an arg slot is strong evidence.

Verdict per slot:
- CONFIRMED: clearly supported (final_name = the role name).
- AMENDED: real role, refined name.
- REFUTED: reused scratch OR not enough evidence -> stays positional (say why in summary).
Each slot's 'summary' (<=140 chars) becomes the symbol comment verbatim.
${seeds}
${CAVEATS}

Return ONLY the structured verdict.`

const slotTally = (vs) => vs.reduce((m, v) => {
  for (const sl of (v.slots || [])) m[sl.verdict] = (m[sl.verdict] || 0) + 1
  return m
}, {})

if (single) {
  phase('Name')
  const out = await pipeline(
    subs,
    (s) => agent(fusedPrompt(s), { label: `name:${s.addr}`, phase: 'Name', schema: VERDICT_SCHEMA }),
  )
  const verified = out.filter(Boolean)
  log(`bank_${bb} (single-agent): ${verified.length} subs, slot verdicts ${JSON.stringify(slotTally(verified))}`)
  return { bank, verified }
}

phase('Propose')
const out = await pipeline(
  subs,
  (s) => agent((s.packet ? packedProposePrompt : proposePrompt)(s), { label: `propose:${s.addr}`, phase: 'Propose', schema: PROPOSE_SCHEMA })
           .then(p => ({ s, p })),
  ({ s, p }) => agent((s.packet ? packedVerifyPrompt : verifyPrompt)(s, p), { label: `verify:${s.addr}`, phase: 'Verify', schema: VERDICT_SCHEMA }),
)

const verified = out.filter(Boolean)
log(`bank_${bb}: ${verified.length} subs verified, slot verdicts ${JSON.stringify(slotTally(verified))}`)
return { bank, verified }
