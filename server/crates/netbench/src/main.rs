//! netbench: load harness for the network server.
//!
//!   netbench sky      a synthetic readsb (JSON position port + HTTP files)
//!   netbench viewers  a crowd of map viewers on the live stream
//!
//! Run the server under test against `sky`, then point `viewers` at it
//! with the server's pid so its CPU and memory land in the report.

mod sky;
mod viewers;
mod wsclient;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Serve a synthetic sky the way readsb does.
    Sky {
        #[arg(long, default_value_t = 10_000)]
        aircraft: usize,
        /// readsb's JSON position port (the API's pushed sky)
        #[arg(long, default_value_t = 30047)]
        json_port: u16,
        /// aircraft.json, clients.json, receivers.json under /data/
        #[arg(long, default_value_t = 8090)]
        http_port: u16,
        #[arg(long, default_value = "127.0.0.1")]
        bind: String,
        #[arg(long, default_value_t = 1)]
        seed: u64,
        /// nothing moves; aircraft.json is constant (plus edge cases) for
        /// byte-for-byte comparisons between servers
        #[arg(long)]
        frozen: bool,
    },
    /// Connect a crowd of viewers and measure what they receive.
    Viewers {
        #[arg(long, default_value = "ws://127.0.0.1:8092/v1/stream")]
        url: String,
        #[arg(long, default_value_t = 100)]
        count: usize,
        /// new connections per second while ramping
        #[arg(long, default_value_t = 200.0)]
        ramp: f64,
        #[arg(long, default_value = "regional=70,country=25,world=5")]
        mix: viewers::Mix,
        /// seconds after the ramp before measuring
        #[arg(long, default_value_t = 10)]
        warmup: u64,
        /// measurement window, seconds
        #[arg(long, default_value_t = 60)]
        duration: u64,
        /// server process(es) to sample from /proc (repeatable)
        #[arg(long)]
        pid: Vec<u32>,
        #[arg(long, default_value_t = 2)]
        seed: u64,
        /// do not offer permessage-deflate (browsers always offer it)
        #[arg(long)]
        no_deflate: bool,
        #[arg(long, default_value = "")]
        label: String,
        /// also write the JSON report here
        #[arg(long)]
        out: Option<std::path::PathBuf>,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    match Cli::parse().cmd {
        Cmd::Sky { aircraft, json_port, http_port, bind, seed, frozen } => {
            sky::run(aircraft, json_port, http_port, seed, &bind, frozen).await
        }
        Cmd::Viewers { url, count, ramp, mix, warmup, duration, pid, seed, no_deflate, label, out } => {
            viewers::run(viewers::Plan {
                url,
                count,
                ramp_per_s: ramp,
                mix,
                warmup_s: warmup,
                duration_s: duration,
                pids: pid,
                seed,
                deflate: !no_deflate,
                label,
                out,
            })
            .await
        }
    }
}
