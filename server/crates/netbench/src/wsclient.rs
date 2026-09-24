//! A minimal WebSocket client that behaves like a browser on the wire:
//! it offers permessage-deflate, so the bytes counted are the bytes a
//! real map receives. Text messages come back decompressed.

use std::io;

use base64::Engine;
use flate2::{Decompress, FlushDecompress};
use tokio::io::{AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

pub struct Client {
    sock: BufReader<TcpStream>,
    inflate: Option<Decompress>,
    /// bytes read off the socket, frame headers included
    pub wire_bytes: u64,
}

pub enum Msg {
    Text(String),
    Binary(usize),
    Closed,
}

fn bad(what: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, what.to_string())
}

impl Client {
    /// `ws://host:port/path`
    pub async fn connect(url: &str, deflate: bool) -> io::Result<Client> {
        let rest = url.strip_prefix("ws://").ok_or_else(|| bad("ws:// urls only"))?;
        let (host, path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, "/"),
        };
        let sock = TcpStream::connect(host).await?;
        sock.set_nodelay(true)?;
        let mut sock = BufReader::with_capacity(16 * 1024, sock);
        let key = base64::engine::general_purpose::STANDARD.encode(rand::random::<[u8; 16]>());
        let ext = if deflate {
            "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
        } else {
            ""
        };
        let req = format!(
            "GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
             Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{ext}\r\n"
        );
        sock.get_mut().write_all(req.as_bytes()).await?;
        // response head, byte by byte to the blank line (it is short)
        let mut head = Vec::with_capacity(256);
        while !head.ends_with(b"\r\n\r\n") {
            let b = sock.read_u8().await?;
            head.push(b);
            if head.len() > 8192 {
                return Err(bad("response head too long"));
            }
        }
        let head = String::from_utf8_lossy(&head).to_lowercase();
        if !head.starts_with("http/1.1 101") {
            return Err(bad(head.lines().next().unwrap_or("no status")));
        }
        let negotiated = head.lines().any(|l| l.starts_with("sec-websocket-extensions:") && l.contains("permessage-deflate"));
        Ok(Client { sock, inflate: negotiated.then(|| Decompress::new(false)), wire_bytes: head.len() as u64 })
    }

    pub async fn send_text(&mut self, text: &str) -> io::Result<()> {
        self.send(0x1, text.as_bytes()).await
    }

    async fn send(&mut self, opcode: u8, p: &[u8]) -> io::Result<()> {
        let mask: [u8; 4] = rand::random();
        let mut f = Vec::with_capacity(p.len() + 14);
        f.push(0x80 | opcode);
        if p.len() < 126 {
            f.push(0x80 | p.len() as u8);
        } else {
            f.push(0x80 | 126);
            f.extend_from_slice(&(p.len() as u16).to_be_bytes());
        }
        f.extend_from_slice(&mask);
        f.extend(p.iter().enumerate().map(|(i, b)| b ^ mask[i & 3]));
        self.sock.get_mut().write_all(&f).await
    }

    pub async fn recv(&mut self) -> io::Result<Msg> {
        let mut payload = Vec::new();
        let mut op = None;
        let mut compressed = false;
        loop {
            let h0 = self.sock.read_u8().await?;
            let h1 = self.sock.read_u8().await?;
            let mut len = (h1 & 0x7f) as u64;
            let mut hl = 2;
            if len == 126 {
                len = self.sock.read_u16().await? as u64;
                hl += 2;
            } else if len == 127 {
                len = self.sock.read_u64().await?;
                hl += 8;
            }
            let start = payload.len();
            payload.resize(start + len as usize, 0);
            self.sock.read_exact(&mut payload[start..]).await?;
            self.wire_bytes += hl + len;
            let this_op = h0 & 0x0f;
            match this_op {
                0x8 => return Ok(Msg::Closed),
                0x9 => {
                    // answer pings, as browsers do (servers close sockets that don't)
                    let ping = payload.split_off(start);
                    self.send(0xA, &ping).await?;
                    continue;
                }
                0xA => {
                    payload.truncate(start);
                    continue;
                }
                0x0 => {}
                _ => {
                    op = Some(this_op);
                    compressed = h0 & 0x40 != 0;
                }
            }
            if h0 & 0x80 != 0 {
                break;
            }
        }
        if compressed {
            let d = self.inflate.as_mut().ok_or_else(|| bad("compressed frame without deflate"))?;
            payload.extend_from_slice(&[0, 0, 0xff, 0xff]);
            let mut out = Vec::with_capacity(payload.len() * 6);
            let mut pos = 0;
            loop {
                if out.capacity() - out.len() < 4096 {
                    out.reserve(out.capacity().max(8192));
                }
                let before = d.total_in();
                d.decompress_vec(&payload[pos..], &mut out, FlushDecompress::Sync).map_err(|_| bad("inflate"))?;
                pos += (d.total_in() - before) as usize;
                // done when the input is used up and the output did not
                // fill the room it had (nothing left pending inside)
                if pos >= payload.len() && out.len() < out.capacity() {
                    break;
                }
            }
            payload = out;
        }
        match op {
            Some(0x1) => Ok(Msg::Text(String::from_utf8(payload).map_err(|_| bad("utf-8"))?)),
            _ => Ok(Msg::Binary(payload.len())),
        }
    }
}
