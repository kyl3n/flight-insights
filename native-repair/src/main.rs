//! Unprivileged capability endpoint. There is deliberately no install, sudo,
//! setuid, arbitrary journal path, qualification flag, or raw command endpoint.
use q22_network_helper::{production_capabilities, Request};
use std::io::{self, BufRead, Read, Write};

fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--capabilities"] {
        println!("{}", production_capabilities());
        return;
    }
    #[cfg(target_os = "linux")]
    if args == ["--serve"] {
        if let Err(error) = q22_network_helper::linux_service::serve() {
            eprintln!("{error}");
            std::process::exit(1)
        }
        return;
    }
    #[cfg(windows)]
    if args == ["--service"] {
        if let Err(error) = q22_network_helper::windows::service_dispatch() {
            eprintln!("{error}");
            std::process::exit(1)
        }
        return;
    }
    if args != ["--stdio"] {
        eprintln!(
            "usage: q22-network-helper --capabilities | --stdio (unprivileged, diagnosis only)"
        );
        std::process::exit(2);
    }
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    loop {
        // `read_line` alone is not a bounded frame decoder.
        let mut frame = Vec::new();
        let count = std::io::Read::by_ref(&mut input)
            .take(65_537)
            .read_until(b'\n', &mut frame);
        let response = match count {
            Ok(0) => break,
            Ok(_) if frame.len() > 65_536 || !frame.ends_with(b"\n") => {
                let _ = writeln!(output, "{}", serde_json::json!({"error":"frame_limit"}));
                break;
            }
            Ok(_) => match serde_json::from_slice::<Request>(&frame) {
                Ok(Request::Capabilities(_)) => {
                    serde_json::json!({"result": production_capabilities()})
                }
                Ok(_) => {
                    serde_json::json!({"error":"diagnosis_only", "capabilities": production_capabilities()})
                }
                Err(_) => serde_json::json!({"error":"invalid_typed_request"}),
            },
            Err(_) => break,
        };
        if writeln!(output, "{response}")
            .and_then(|_| output.flush())
            .is_err()
        {
            break;
        }
    }
}
