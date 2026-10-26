-- bridge.lua: runs inside Mesen 2. Connects to the Python client over TCP and executes
-- newline-delimited commands, one reply line per command. Console-agnostic: it knows
-- frames, input tables, memory types and savestates, never what a button or byte means.
--
-- Lockstep: the emulator only runs frames the client asked for. Between commands the
-- script blocks inside a one-shot exec callback armed at endFrame, so it waits at the
-- frame boundary (the instruction right after endFrame, before the next inputPolled).
-- An exec callback is also the only place Mesen allows savestates, so save/load work
-- whenever the client can talk. Verified by spike/probe_events.lua (2026-10-01):
-- event order is endFrame -> inputPolled -> startFrame -> CPU -> endFrame, and the
-- one-shot callback fires before the next inputPolled.
--
-- Mesen's ScriptTimeout applies per callback, so each block must end within it
-- (GUI maximum 100 s): the client must send its next command within that time.
--
-- Commands:
--   ping                     -> pong
--   info                     -> frame=<n> sha1=<rom file sha1> name=<rom name>
--   keys <names>             -> set the button order for masks (comma list, getInput names); ok
--   inputkeys <port>         -> comma list of the button names getInput(port) reports
--   step <n> <mask> [port]   -> hold <mask> for the next n input polls; frame=<n>
--   seq <port> <hexmasks>    -> one mask per input poll (2 hex digits each, <= 8 buttons); frame=<n>
--   (Mesen polls input once per frame, lag frames included, except the partial frame
--   from power-on to the first endFrame, so from power-on frame = masks + 1.)
--   read <memType> <addr> <len> -> hex bytes (memType is an emu.memType name, e.g. nesDebug)
--   save                     -> hex of a savestate taken at this frame boundary
--   load <hex>               -> restore it; frame=<n> (the frame counter rewinds with it)
--   trace <memType> <addr> <len> <path>[|<flagAddr>] -> from now on, at every endFrame append
--                               1 flag byte (flagAddr was read this frame) + len bytes; ok
--   trace off                -> close the trace file; ok
--   cdl <memType> <path>     -> write emu.getCdlData(memType) (one flag byte per byte of that
--                               memory: the code/data log so far) to path; ok <length>
--   write <memType> <addr> <hex> -> write the bytes; ok <count>
--   shot <path>              -> the console's own rendered frame as PNG (emu.takeScreenshot,
--                               never the desktop) written to path; ok <bytes>
--   state [prefix]           -> emu.getState() scalars as sorted key=value pairs (e.g. "cpu.")
--   watch <exec|read|write> <start> <end> <path> [regs] [memType:addr:len ...]
--                            -> from now on, one text line per hit: frame address value, the cpu.*
--                               state if "regs", hex of each range; first line names the columns.
--                               ok <id>. "watch off [id]" closes one or all.
--   quit                     -> bye, then emu.stop(0)

local socket = require("socket.core")

local port = tonumber(os.getenv("MESEN_HARNESS_PORT") or "")
assert(port, "bridge: MESEN_HARNESS_PORT not set")

local conn = socket.tcp()
conn:settimeout(15)
assert(conn:connect("127.0.0.1", port))
conn:setoption("tcp-nodelay", true)
conn:settimeout(nil)

local frame = 0          -- endFrame events since power-on (or since the loaded state's frame)
local keys = {}          -- button order: bit i of a mask = keys[i+1]
local queue = {}         -- per-frame masks still to apply
local qport = 0
local qhead = 1
local pending = nil      -- reply owed once the queued frames have run
local applied = false    -- a mask was applied at this frame's inputPolled

local function send(line) assert(conn:send(line .. "\n")) end

local function mask_to_input(mask)
  local t = {}
  for i, name in ipairs(keys) do t[name] = (mask >> (i - 1)) & 1 == 1 end
  return t
end

local function tohex(s)
  return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

local function fromhex(h)
  return (h:gsub("%x%x", function(x) return string.char(tonumber(x, 16)) end))
end

-- Per-frame memory trace, written at each endFrame: 1 flag byte (was `flagAddr` read
-- during the frame; 0 if no flag address given) + `len` bytes from `addr`.
local trace = nil

local function trace_open(mt, addr, len, path, flagAddr)
  local memType = assert(emu.memType[mt], "trace: unknown memType " .. tostring(mt))
  trace = { memType = memType, addr = addr, len = len, f = assert(io.open(path, "wb")),
            read = false, buf = {} }
  if flagAddr then
    trace.flagAddr = flagAddr
    trace.cb = emu.addMemoryCallback(function() trace.read = true end,
      emu.callbackType.read, flagAddr, flagAddr)
  end
end

local function trace_close()
  if not trace then return end
  if trace.cb then emu.removeMemoryCallback(trace.cb, emu.callbackType.read, trace.flagAddr, trace.flagAddr) end
  trace.f:close()
  trace = nil
end

local function trace_frame()
  local b, a, mt = trace.buf, trace.addr, trace.memType
  for i = 1, trace.len do b[i] = emu.read(a + i - 1, mt) end
  trace.f:write(string.char(trace.read and 1 or 0), string.char(table.unpack(b, 1, trace.len)))
  trace.read = false
end

-- emu.getState() as one line of sorted key=value pairs (scalars only), optionally only the
-- keys starting with `prefix` (e.g. "cpu."). Key names are Mesen's and vary by console.
function state_line(prefix)
  local st, ks = emu.getState(), {}
  for k, v in pairs(st) do
    if type(v) ~= "table" and (not prefix or k:sub(1, #prefix) == prefix) then ks[#ks + 1] = k end
  end
  table.sort(ks)
  local out = {}
  for _, k in ipairs(ks) do out[#out + 1] = k .. "=" .. tostring(st[k]) end
  return table.concat(out, " ")
end

-- Watches: a memory callback that appends one text line per hit, so a game probe is a spec,
-- not a script ("log now, calc later"). Line: frame address value [cpu.* values] [hex per range].
-- The first line of the file, "# ...", names the columns.
local watches, watch_next = {}, 1

function watch_open(spec)
  -- <exec|read|write> <start> <end> <path> [regs] [<memType>:<addr>:<len> ...]
  local kind, s, e, path, opts = spec:match("^(%a+)%s+(%d+)%s+(%d+)%s+(%S+)%s*(.*)$")
  assert(kind and emu.callbackType[kind], "watch: usage watch <exec|read|write> <start> <end> <path> [regs] [memType:addr:len ...]")
  local w = { kind = emu.callbackType[kind], s = tonumber(s), e = tonumber(e),
              f = assert(io.open(path, "w")), regs = nil, ranges = {} }
  local cols = { "frame", "address", "value" }
  for tok in opts:gmatch("%S+") do
    if tok == "regs" then
      w.regs = {}
      for kv in state_line("cpu."):gmatch("%S+") do
        local k = kv:match("^([^=]+)=")
        w.regs[#w.regs + 1] = k
        cols[#cols + 1] = k
      end
    else
      local mt, a, l = tok:match("^(%a+):(%d+):(%d+)$")
      assert(mt and emu.memType[mt], "watch: bad range " .. tok)
      w.ranges[#w.ranges + 1] = { emu.memType[mt], tonumber(a), tonumber(l) }
      cols[#cols + 1] = tok
    end
  end
  w.f:write("# " .. table.concat(cols, " ") .. "\n")
  w.cb = emu.addMemoryCallback(function(address, value)
    local parts = { frame, address, value or 0 }
    if w.regs then
      local st = emu.getState()
      for _, k in ipairs(w.regs) do parts[#parts + 1] = tostring(st[k]) end
    end
    for _, r in ipairs(w.ranges) do
      local b = {}
      for i = 0, r[3] - 1 do b[#b + 1] = string.format("%02x", emu.read(r[2] + i, r[1])) end
      parts[#parts + 1] = table.concat(b)
    end
    w.f:write(table.concat(parts, " "), "\n")
  end, w.kind, w.s, w.e)
  local id = watch_next
  watches[id], watch_next = w, watch_next + 1
  return id
end

function watch_close(id)
  for i, w in pairs(watches) do
    if not id or i == id then
      emu.removeMemoryCallback(w.cb, w.kind, w.s, w.e)
      w.f:close()
      watches[i] = nil
    end
  end
end

-- Returns true when the client asked to run frames (leave the callback and emulate).
local function handle(line)
  local cmd, rest = line:match("^(%S+)%s*(.*)$")
  if cmd == "ping" then
    send("pong")
  elseif cmd == "info" then
    local r = emu.getRomInfo()
    send(string.format("frame=%d sha1=%s name=%s", frame, r.fileSha1Hash, r.name))
  elseif cmd == "keys" then
    keys = {}
    for k in rest:gmatch("[^,]+") do keys[#keys + 1] = k end
    send("ok")
  elseif cmd == "inputkeys" then
    local names = {}
    for k in pairs(emu.getInput(tonumber(rest) or 0)) do names[#names + 1] = k end
    table.sort(names)
    send(table.concat(names, ","))
  elseif cmd == "step" then
    local n, mask, p = rest:match("^(%d+)%s+(%d+)%s*(%d*)$")
    n, mask = tonumber(n), tonumber(mask)
    assert(n, "step: usage step <n> <mask> [port]")
    if n == 0 then send("frame=" .. frame) return false end
    queue, qhead, qport = {}, 1, tonumber(p) or 0
    for i = 1, n do queue[i] = mask end
    pending = true
    return true
  elseif cmd == "seq" then
    local p, hex = rest:match("^(%d+)%s+(%x*)$")
    assert(p and #hex % 2 == 0, "seq: usage seq <port> <hexmasks>")
    if #hex == 0 then send("frame=" .. frame) return false end
    queue, qhead, qport = {}, 1, tonumber(p)
    for x in hex:gmatch("%x%x") do queue[#queue + 1] = tonumber(x, 16) end
    pending = true
    return true
  elseif cmd == "read" then
    local mt, a, l = rest:match("^(%S+)%s+(%d+)%s+(%d+)$")
    local memType = assert(emu.memType[mt or ""], "read: unknown memType " .. tostring(mt))
    a, l = tonumber(a), tonumber(l)
    local out = {}
    for i = 0, l - 1 do out[#out + 1] = string.format("%02x", emu.read(a + i, memType)) end
    send(table.concat(out))
  elseif cmd == "write" then
    local mt, a, hex = rest:match("^(%S+)%s+(%d+)%s+(%x+)$")
    local memType = assert(emu.memType[mt or ""], "write: unknown memType " .. tostring(mt))
    a = tonumber(a)
    local i = 0
    for x in hex:gmatch("%x%x") do emu.write(a + i, tonumber(x, 16), memType); i = i + 1 end
    send("ok " .. i)
  elseif cmd == "shot" then
    -- the console's own frame (what the emulator rendered), never the desktop
    local png = emu.takeScreenshot()
    local f = assert(io.open(rest, "wb"))
    f:write(png)
    f:close()
    send("ok " .. #png)
  elseif cmd == "state" then
    send(state_line(rest ~= "" and rest or nil))
  elseif cmd == "watch" then
    if rest:match("^off") then
      watch_close(tonumber(rest:match("^off%s+(%d+)")))
      send("ok")
    else
      send("ok " .. watch_open(rest))
    end
  elseif cmd == "save" then
    local st = emu.createSavestate()
    -- the frame counter lives in Lua, not in the savestate: carry it in a 4-byte prefix
    send(string.format("%08x", frame) .. tohex(st))
  elseif cmd == "load" then
    assert(#rest > 8, "load: usage load <hex from save>")
    frame = tonumber(rest:sub(1, 8), 16)
    emu.loadSavestate(fromhex(rest:sub(9)))
    queue, qhead, applied = {}, 1, false
    send("frame=" .. frame)
  elseif cmd == "trace" then
    if rest == "off" then
      trace_close()
    else
      local mt, a, l, path = rest:match("^(%S+)%s+(%d+)%s+(%d+)%s+(.+)$")
      assert(path, "trace: usage trace <memType> <addr> <len> <path> | trace off")
      local p, fa = path:match("^(.-)%s*|%s*(%d+)$")
      if p then path = p end
      trace_close()
      trace_open(mt, tonumber(a), tonumber(l), path, tonumber(fa))
    end
    send("ok")
  elseif cmd == "cdl" then
    local mt, path = rest:match("^(%S+)%s+(.+)$")
    local memType = assert(emu.memType[mt or ""], "cdl: unknown memType " .. tostring(mt))
    local data = emu.getCdlData(memType)
    -- Mesen hands back a 0-indexed table: # misses element 0, and table.unpack from 1 would
    -- drop the first byte and shift every flag by one. Write from the real first index.
    local first = data[0] ~= nil and 0 or 1
    local last = #data
    local f = assert(io.open(path, "wb"))
    for i = first, last, 4096 do
      f:write(string.char(table.unpack(data, i, math.min(i + 4095, last))))
    end
    f:close()
    send("ok " .. (last - first + 1))
  elseif cmd == "quit" then
    trace_close()
    watch_close()
    send("bye")
    conn:close()
    emu.stop(0)
    return true
  else
    send("err unknown command: " .. tostring(cmd))
  end
  return false
end

-- Block at this frame boundary until the client asks for frames.
local function serve()
  if pending then
    pending = nil
    send("frame=" .. frame)
  end
  while true do
    local line, err = conn:receive("*l")
    if not line then
      emu.log("bridge: connection " .. tostring(err) .. ", stopping")
      emu.stop(1)
      return
    end
    local ok, run = pcall(handle, line)
    if not ok then
      send("err " .. tostring(run):gsub("\n", " "))
    elseif run then
      return
    end
  end
end

local armed = nil
local function arm()
  if armed then return end
  armed = emu.addMemoryCallback(function()
    emu.removeMemoryCallback(armed, emu.callbackType.exec, 0x0000, 0xFFFF)
    armed = nil
    serve()
  end, emu.callbackType.exec, 0x0000, 0xFFFF)
end

emu.addEventCallback(function()
  if qhead <= #queue then
    emu.setInput(mask_to_input(queue[qhead]), qport)
    applied = true
  end
end, emu.eventType.inputPolled)

emu.addEventCallback(function()
  frame = frame + 1
  if trace then trace_frame() end
  -- A mask is consumed by an inputPolled, not by a frame: the partial frame from
  -- power-on to the first endFrame has no inputPolled, so it must not eat mask #1.
  if applied then qhead = qhead + 1; applied = false end
  if qhead > #queue then arm() end
end, emu.eventType.endFrame)

-- Block on the very first instruction, before frame 0 runs.
arm()
