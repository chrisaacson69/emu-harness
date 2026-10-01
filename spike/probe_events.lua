-- Probe (spike, not product): record the order of Mesen frame events for the first
-- few frames, then test whether an exec callback can remove itself. Run headless:
--   Mesen.exe --testRunner probe_events.lua <rom>
-- Writes to the file named by env MH_OUT. NES-specific bits (NMI vector) are probe-only.
local out = assert(io.open(assert(os.getenv("MH_OUT"), "MH_OUT not set"), "w"))
local function log(s) out:write(s, "\n"); out:flush() end

local socket_ok = pcall(require, "socket.core")
log("socket.core available=" .. tostring(socket_ok))

local nmi = emu.read16(0xFFFA, emu.memType.nesDebug)
local rst = emu.read16(0xFFFC, emu.memType.nesDebug)
log(string.format("vectors nmi=$%04X reset=$%04X", nmi, rst))

local frame, seq = 0, 0
local function ev(name) seq = seq + 1; log(string.format("%4d f=%d %s", seq, frame, name)) end
local LIMIT = 6

emu.addEventCallback(function() ev("startFrame") end, emu.eventType.startFrame)
emu.addEventCallback(function()
  ev("endFrame"); frame = frame + 1
  if frame == LIMIT then
    -- self-removal test: one-shot exec callback over the whole CPU space
    local ref
    ref = emu.addMemoryCallback(function(addr)
      log(string.format("oneshot exec at $%04X", addr))
      local ok, st = pcall(emu.createSavestate)
      log("createSavestate in exec: ok=" .. tostring(ok) .. " len=" .. tostring(ok and #st or st))
      emu.removeMemoryCallback(ref, emu.callbackType.exec, 0x0000, 0xFFFF)
      log("removed self")
    end, emu.callbackType.exec, 0x0000, 0xFFFF)
  elseif frame == LIMIT + 3 then
    log("done, stopping"); out:close(); emu.stop(42)
  end
end, emu.eventType.endFrame)
emu.addEventCallback(function() ev("inputPolled") end, emu.eventType.inputPolled)
emu.addEventCallback(function() ev("nmi") end, emu.eventType.nmi)
emu.addMemoryCallback(function() if frame < LIMIT then ev("exec NMI handler") end end,
  emu.callbackType.exec, nmi, nmi)
log("probe loaded")
