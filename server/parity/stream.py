"""First frames of /v1/stream from both servers, for the same boxes."""
import asyncio, json, sys
import websockets

PY, RS = sys.argv[1], sys.argv[2]
BOXES = [None, [-10, 35, 30, 60], [170, -60, -170, 60], [0, 0, 1, 1]]

async def first(url, box, send=True, compress="deflate"):
    async with websockets.connect(url, compression=compress) as ws:
        if send:
            await ws.send(json.dumps({"bbox": box}))
        return await asyncio.wait_for(ws.recv(), 5)

async def main():
    bad = 0
    for box in BOXES:
        for send in (True, False):
            if not send and box is not None:
                continue
            a = await first(PY + "/v1/stream", box, send)
            b = await first(RS + "/v1/stream", box, send)
            same = a == b
            bad += not same
            print("%s box=%s sent=%s  %d bytes" % ("ok  " if same else "DIFF", box, send, len(a)))
            if not same:
                print("   py", a[:200]); print("   rs", b[:200])
    # no deflate offered
    a = await first(PY + "/v1/stream", [-10, 35, 30, 60], True, None)
    b = await first(RS + "/v1/stream", [-10, 35, 30, 60], True, None)
    print("%s uncompressed" % ("ok  " if a == b else "DIFF")); bad += a != b
    # a broken message closes with 1003 on both
    for base in (PY, RS):
        async with websockets.connect(base + "/v1/stream") as ws:
            await ws.send("not json")
            try:
                await asyncio.wait_for(ws.recv(), 5)
            except websockets.ConnectionClosed as e:
                print("close", base[-4:], e.rcvd.code if e.rcvd else None)
    print("%d differ" % bad)

asyncio.run(main())
