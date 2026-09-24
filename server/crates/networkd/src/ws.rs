//! A small server-side WebSocket (RFC 6455) over hyper's upgrade.
//!
//! Owned here rather than borrowed from a library for two reasons: the
//! memory a connection costs is ours to bound (10k sockets on a small
//! board), and server frames are unmasked, so one encoded frame can be
//! written unchanged to every socket that should receive it.
//!
//! permessage-deflate (RFC 7692) is accepted when offered, always without
//! context takeover in either direction: no compressor state lives with a
//! connection, and each message is compressed on its own.

use std::cell::RefCell;

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderValue, Response, StatusCode};
use base64::Engine;
use bytes::{Bytes, BytesMut};
use flate2::{Compress, Compression, Decompress, FlushCompress, FlushDecompress};
use sha1::{Digest, Sha1};
use tokio::io::{AsyncRead, AsyncReadExt};

const GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
pub const MAX_MESSAGE: usize = 64 * 1024;

pub const OP_CONT: u8 = 0x0;
pub const OP_TEXT: u8 = 0x1;
pub const OP_BINARY: u8 = 0x2;
pub const OP_CLOSE: u8 = 0x8;
pub const OP_PING: u8 = 0x9;
pub const OP_PONG: u8 = 0xA;

pub struct Handshake {
    pub response: Response<Body>,
    pub deflate: bool,
    pub takeover: bool,
}

fn has_token(headers: &HeaderMap, name: header::HeaderName, token: &str) -> bool {
    headers.get_all(name).iter().filter_map(|v| v.to_str().ok()).any(|v| {
        v.split(',').any(|t| t.trim().eq_ignore_ascii_case(token))
    })
}

/// Validate an upgrade request; None when it is not a WebSocket request.
/// `takeover`: keep a compressor per connection (better ratio, more
/// memory) instead of compressing each message on its own.
pub fn handshake(headers: &HeaderMap, takeover: bool) -> Option<Handshake> {
    if !has_token(headers, header::UPGRADE, "websocket") || !has_token(headers, header::CONNECTION, "upgrade") {
        return None;
    }
    if headers.get(header::SEC_WEBSOCKET_VERSION).and_then(|v| v.to_str().ok()) != Some("13") {
        return None;
    }
    let key = headers.get(header::SEC_WEBSOCKET_KEY)?.to_str().ok()?;
    let mut h = Sha1::new();
    h.update(key.trim().as_bytes());
    h.update(GUID.as_bytes());
    let accept = base64::engine::general_purpose::STANDARD.encode(h.finalize());

    let deflate = headers
        .get_all(header::SEC_WEBSOCKET_EXTENSIONS)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .any(|offer| offer.split(';').next().is_some_and(|n| n.trim() == "permessage-deflate"));

    let mut r = Response::new(Body::empty());
    *r.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
    let hm = r.headers_mut();
    hm.insert(header::UPGRADE, HeaderValue::from_static("websocket"));
    hm.insert(header::CONNECTION, HeaderValue::from_static("Upgrade"));
    hm.insert(header::SEC_WEBSOCKET_ACCEPT, HeaderValue::from_str(&accept).ok()?);
    if deflate {
        hm.insert(
            header::SEC_WEBSOCKET_EXTENSIONS,
            HeaderValue::from_static(if takeover {
                "permessage-deflate; client_no_context_takeover"
            } else {
                "permessage-deflate; server_no_context_takeover; client_no_context_takeover"
            }),
        );
    }
    Some(Handshake { response: r, deflate, takeover: deflate && takeover })
}

thread_local! {
    static COMPRESS: RefCell<Compress> = RefCell::new(Compress::new(Compression::new(1), false));
}

/// Raw deflate of one message, flushed and with the 00 00 ff ff tail
/// removed (RFC 7692 §7.2.1).
pub fn deflate(payload: &[u8]) -> Vec<u8> {
    COMPRESS.with(|c| {
        let mut c = c.borrow_mut();
        c.reset();
        // zlib's compressBound plus the flush marker: room to finish in one call
        let n = payload.len();
        let mut out = Vec::with_capacity(n + (n >> 12) + (n >> 14) + (n >> 25) + 64);
        c.compress_vec(payload, &mut out, FlushCompress::Sync).expect("deflate");
        debug_assert_eq!(c.total_in() as usize, n);
        debug_assert!(out.ends_with(&[0, 0, 0xff, 0xff]));
        out.truncate(out.len().saturating_sub(4));
        out
    })
}

pub fn inflate(payload: &[u8]) -> Option<Vec<u8>> {
    let mut d = Decompress::new(false);
    let mut input = payload.to_vec();
    input.extend_from_slice(&[0, 0, 0xff, 0xff]);
    let mut out = Vec::with_capacity(payload.len() * 4 + 64);
    let mut pos = 0;
    loop {
        if out.capacity() - out.len() < 1024 {
            out.reserve(out.capacity().max(4096));
        }
        let before = d.total_in();
        d.decompress_vec(&input[pos..], &mut out, FlushDecompress::Sync).ok()?;
        pos += (d.total_in() - before) as usize;
        if out.len() > MAX_MESSAGE {
            return None;
        }
        if pos >= input.len() {
            return Some(out);
        }
    }
}

/// Room to leave at the front of a buffer for the largest frame header.
pub const HEADROOM: usize = 10;

fn header(opcode: u8, n: usize, compressed: bool) -> ([u8; HEADROOM], usize) {
    let mut h = [0u8; HEADROOM];
    h[0] = 0x80 | if compressed { 0x40 } else { 0 } | opcode;
    let len = if n < 126 {
        h[1] = n as u8;
        2
    } else if n <= 0xffff {
        h[1] = 126;
        h[2..4].copy_from_slice(&(n as u16).to_be_bytes());
        4
    } else {
        h[1] = 127;
        h[2..10].copy_from_slice(&(n as u64).to_be_bytes());
        10
    };
    (h, len)
}

/// One complete server frame: header plus payload, ready to write.
pub fn frame(opcode: u8, payload: &[u8], compressed: bool) -> Bytes {
    let (h, hl) = header(opcode, payload.len(), compressed);
    let mut b = BytesMut::with_capacity(payload.len() + hl);
    b.extend_from_slice(&h[..hl]);
    b.extend_from_slice(payload);
    b.freeze()
}

/// A frame around a payload written after HEADROOM bytes of `buf`,
/// without copying the payload.
pub fn frame_in_place(opcode: u8, mut buf: Vec<u8>) -> Bytes {
    let n = buf.len() - HEADROOM;
    let (h, hl) = header(opcode, n, false);
    let start = HEADROOM - hl;
    buf[start..HEADROOM].copy_from_slice(&h[..hl]);
    Bytes::from(buf).slice(start..)
}

/// How a connection compresses what it sends.
pub enum Out {
    Plain,
    /// each message on its own (no state kept between messages)
    Fresh,
    /// one compressor for the connection's life: later messages refer
    /// back to earlier ones
    Takeover(Box<Compress>),
}

impl Out {
    pub fn new(deflate: bool, takeover: bool) -> Out {
        match (deflate, takeover) {
            (false, _) => Out::Plain,
            (true, false) => Out::Fresh,
            (true, true) => Out::Takeover(Box::new(Compress::new(Compression::new(1), false))),
        }
    }

    /// A text frame for `payload`.
    pub fn text(&mut self, payload: &[u8]) -> Bytes {
        match self {
            Out::Plain => frame(OP_TEXT, payload, false),
            Out::Fresh => frame(OP_TEXT, &deflate(payload), true),
            Out::Takeover(c) => {
                let n = payload.len();
                let mut z = Vec::with_capacity(n + (n >> 12) + (n >> 14) + (n >> 25) + 64);
                let before = c.total_in();
                c.compress_vec(payload, &mut z, FlushCompress::Sync).expect("deflate");
                debug_assert_eq!((c.total_in() - before) as usize, n);
                z.truncate(z.len().saturating_sub(4));
                frame(OP_TEXT, &z, true)
            }
        }
    }

    /// A text frame for a payload written after HEADROOM bytes of `buf`.
    pub fn text_in_place(&mut self, buf: Vec<u8>) -> Bytes {
        match self {
            Out::Plain => frame_in_place(OP_TEXT, buf),
            _ => self.text(&buf[HEADROOM..]),
        }
    }
}

pub fn close_frame(code: u16) -> Bytes {
    frame(OP_CLOSE, &code.to_be_bytes(), false)
}

/// What the client sent.
pub enum Incoming {
    Text(Vec<u8>),
    Binary,
    Ping(Vec<u8>),
    Close,
    /// Protocol error or oversize: close with this code.
    Fail(u16),
}

/// Read the next complete message (control frames surface as they come).
pub async fn read_message<R: AsyncRead + Unpin>(r: &mut R, deflate_on: bool) -> std::io::Result<Incoming> {
    let mut msg: Vec<u8> = vec![];
    let mut msg_op: Option<u8> = None;
    let mut compressed = false;
    loop {
        let mut h = [0u8; 2];
        r.read_exact(&mut h).await?;
        let fin = h[0] & 0x80 != 0;
        let rsv1 = h[0] & 0x40 != 0;
        let op = h[0] & 0x0f;
        let masked = h[1] & 0x80 != 0;
        let mut len = (h[1] & 0x7f) as u64;
        if len == 126 {
            let mut b = [0u8; 2];
            r.read_exact(&mut b).await?;
            len = u16::from_be_bytes(b) as u64;
        } else if len == 127 {
            let mut b = [0u8; 8];
            r.read_exact(&mut b).await?;
            len = u64::from_be_bytes(b);
        }
        if !masked || h[0] & 0x30 != 0 || (rsv1 && !deflate_on) {
            return Ok(Incoming::Fail(1002));
        }
        if len as usize > MAX_MESSAGE || msg.len() + len as usize > MAX_MESSAGE {
            return Ok(Incoming::Fail(1009));
        }
        let mut mask = [0u8; 4];
        r.read_exact(&mut mask).await?;
        let mut payload = vec![0u8; len as usize];
        r.read_exact(&mut payload).await?;
        for (i, b) in payload.iter_mut().enumerate() {
            *b ^= mask[i & 3];
        }
        match op {
            OP_PING => return Ok(Incoming::Ping(payload)),
            OP_PONG => continue,
            OP_CLOSE => return Ok(Incoming::Close),
            OP_TEXT | OP_BINARY => {
                if msg_op.is_some() {
                    return Ok(Incoming::Fail(1002));
                }
                msg_op = Some(op);
                compressed = rsv1;
                msg = payload;
            }
            OP_CONT => {
                if msg_op.is_none() {
                    return Ok(Incoming::Fail(1002));
                }
                msg.extend_from_slice(&payload);
            }
            _ => return Ok(Incoming::Fail(1002)),
        }
        if fin {
            break;
        }
    }
    if compressed {
        msg = match inflate(&msg) {
            Some(m) => m,
            None => return Ok(Incoming::Fail(1009)),
        };
    }
    Ok(match msg_op {
        Some(OP_TEXT) => Incoming::Text(msg),
        _ => Incoming::Binary,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deflate_round_trips() {
        let msg = br#"{"t":1.5,"upd":[{"hex":"abc123","lat":1.3},{"hex":"abc124","lat":1.3}]}"#.repeat(50);
        let z = deflate(&msg);
        assert!(z.len() < msg.len() / 5);
        assert_eq!(inflate(&z).unwrap(), msg);
        // twice: the thread's compressor starts clean every message
        assert_eq!(inflate(&deflate(&msg)).unwrap(), msg);
    }

    #[test]
    fn takeover_stream_inflates_in_order() {
        let mut out = Out::new(true, true);
        let mut d = Decompress::new(false);
        for i in 0..20 {
            let msg = format!(r#"{{"t":{i},"upd":[{{"hex":"abc123","lat":{i}.5}}]}}"#).repeat(3);
            let f = out.text(msg.as_bytes());
            let hl = if f[1] & 0x7f == 126 { 4 } else { 2 };
            let mut z = f[hl..].to_vec();
            z.extend_from_slice(&[0, 0, 0xff, 0xff]);
            let mut got = Vec::with_capacity(4096);
            d.decompress_vec(&z, &mut got, FlushDecompress::Sync).unwrap();
            assert_eq!(got, msg.as_bytes(), "message {i}");
        }
    }

    #[test]
    fn frames_in_place_match() {
        for n in [0usize, 5, 125, 126, 300, 65535, 65536, 70000] {
            let payload = vec![7u8; n];
            let mut buf = vec![0u8; HEADROOM];
            buf.extend_from_slice(&payload);
            assert_eq!(frame_in_place(OP_TEXT, buf), frame(OP_TEXT, &payload, false), "{n}");
        }
    }

    #[test]
    fn frame_lengths() {
        assert_eq!(&frame(OP_TEXT, b"hi", false)[..], &[0x81, 2, b'h', b'i']);
        let f = frame(OP_TEXT, &[0u8; 300], true);
        assert_eq!(&f[..4], &[0xC1, 126, 1, 44]);
        assert_eq!(frame(OP_BINARY, &vec![0u8; 70000], false)[1], 127);
    }

    #[tokio::test]
    async fn reads_masked_fragmented_text() {
        // "Hel" + "lo", masked
        let mask = [1u8, 2, 3, 4];
        let mut wire = vec![0x01, 0x83];
        wire.extend_from_slice(&mask);
        wire.extend(b"Hel".iter().enumerate().map(|(i, b)| b ^ mask[i & 3]));
        wire.extend_from_slice(&[0x80, 0x82]);
        wire.extend_from_slice(&mask);
        wire.extend(b"lo".iter().enumerate().map(|(i, b)| b ^ mask[i & 3]));
        let mut r = &wire[..];
        match read_message(&mut r, false).await.unwrap() {
            Incoming::Text(t) => assert_eq!(t, b"Hello"),
            _ => panic!("expected text"),
        }
    }
}
