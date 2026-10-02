-- Play a loaded .bk2 (EmuHawk --movie=... --lua=this) and write a per-frame RAM trace:
-- for each frame advanced, 1 byte lag flag + 2048 bytes of system RAM ($0000-$07FF).
-- Env: TRACE_OUT (output path), TRACE_FRAMES (max frames, default whole movie).
-- The reference oracle for cross-emulator drift: compare with Mesen's bridge `trace`.
local path = assert(os.getenv("TRACE_OUT"), "TRACE_OUT not set")
local out = assert(io.open(path, "wb"))
local status = io.open(path .. ".txt", "w")
local function log(s) status:write(s, "\n"); status:flush(); console.log(s) end

log("movie loaded=" .. tostring(movie.isloaded()) .. " length=" .. tostring(movie.length()))
if not movie.isloaded() then log("NO MOVIE"); status:close(); client.exit() end
for k, v in pairs(movie.getheader()) do log("header " .. tostring(k) .. "=" .. tostring(v)) end
movie.setreadonly(true)

local limit = tonumber(os.getenv("TRACE_FRAMES") or "") or movie.length()
log("start framecount=" .. emu.framecount())
client.unpause()
-- Mirror the AIBeatsZelda bridge's `fast` mode (it ran this BizHawk version for hours):
-- unthrottled, sound off.
for _, f in ipairs({ function() emu.limitframerate(false) end,
                     function() client.SetSoundOn(false) end }) do
  local ok, e = pcall(f)
  if not ok then log("setup call failed: " .. tostring(e)) end
end

local chars = {}
local pauses = 0
while emu.framecount() < limit do
  -- Three traces stopped early (2026-10-02). Two candidate causes, not separated: the Lua
  -- Console window was being closed (that stops this script), and PauseWhenMenuActivated=true.
  -- The run that completed had the console left open AND that setting off. Keep the Lua
  -- Console open. (This check cannot catch a pause that blocks frameadvance; it only logs.)
  if client.ispaused() then
    pauses = pauses + 1
    log("unpausing at framecount=" .. emu.framecount())
    client.unpause()
  end
  emu.frameadvance()
  local arr = mainmemory.read_bytes_as_array(0, 0x800)
  local base = (arr[0] ~= nil) and 0 or 1
  chars[1] = string.char(emu.islagged() and 1 or 0)
  for i = 0, 0x7FF do chars[i + 2] = string.char(arr[i + base]) end
  out:write(table.concat(chars))
end
out:close()
log(string.format("done framecount=%d lag=%d pauses=%d", emu.framecount(), emu.lagcount(), pauses))
status:close()
client.exit()
