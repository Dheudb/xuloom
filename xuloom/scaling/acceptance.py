"""Run an owner-supplied acceptance command with bounded output and owned cleanup."""
import asyncio

from xuloom.utils.shell import decode_output, process_options, terminate_process_tree


async def run_command(spec, stop):
    try:
        process = await asyncio.create_subprocess_shell(
            spec["command"], cwd=spec["cwd"], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **process_options(),
        )
    except OSError as error:
        return "failed", None, "Acceptance command could not start: " + str(error)

    async def collect():
        tail = bytearray()
        while chunk := await process.stdout.read(16384):
            tail.extend(chunk)
            del tail[:-32000]
        return await process.wait(), decode_output(tail)[-8000:]

    output = asyncio.create_task(collect())
    cancelled = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({output, cancelled}, timeout=spec["timeout"], return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done:
            return "cancelled", None, ""
        if output in done:
            code, text = output.result()
            return "passed" if code == 0 else "failed", code, text
        return "failed", None, f"Acceptance command timed out after {spec['timeout']} seconds"
    finally:
        try:
            if not output.done():
                await terminate_process_tree(process)
        finally:
            output.cancel()
            cancelled.cancel()
            await asyncio.gather(output, cancelled, return_exceptions=True)
