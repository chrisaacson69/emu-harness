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
  elseif cmd == "quit" then
    trace_close()
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
