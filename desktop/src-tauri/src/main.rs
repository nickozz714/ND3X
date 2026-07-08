// Prevents an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{SocketAddr, TcpStream};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Port the bundled backend listens on (localhost only).
const PORT: u16 = 8765;

/// Holds the backend child so we can kill it when the app exits.
struct Backend(Mutex<Option<CommandChild>>);

fn wait_for_port(port: u16, timeout: Duration) -> bool {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let start = Instant::now();
    while start.elapsed() < timeout {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();

            // Per-user data dir for the backend (DB, config, secrets live here).
            let data_dir = app
                .path()
                .app_data_dir()
                .unwrap_or_else(|_| std::env::temp_dir());
            let nd3x_home = data_dir.join("nd3x");
            std::fs::create_dir_all(&nd3x_home).ok();

            // Launch the bundled PyInstaller backend as a sidecar.
            let (mut rx, child) = app
                .shell()
                .sidecar("nd3x-backend")
                .expect("nd3x-backend sidecar is missing from the bundle")
                .env("ND3X_HOST", "127.0.0.1")
                .env("ND3X_PORT", PORT.to_string())
                .env("ND3X_HOME", nd3x_home.to_string_lossy().to_string())
                .env("ND3X_BASE_DIR", nd3x_home.to_string_lossy().to_string())
                // Run the backend FROM the per-user data dir: it writes some paths
                // (logs/, files/, db/) relative to the working directory, and a
                // Finder-launched app inherits CWD "/" (read-only) → the engine would
                // crash on startup. This makes those writes land in a writable place.
                .current_dir(nd3x_home.clone())
                // Mark this as the local desktop app so the backend can offer
                // interactive browser login (loopback redirect works here).
                .env("ND3X_DESKTOP", "1")
                .spawn()
                .expect("failed to spawn the ND3X backend");
            app.state::<Backend>().0.lock().unwrap().replace(child);

            // Surface backend stdout/stderr to the host console (helpful while iterating).
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                            eprint!("[backend] {}", String::from_utf8_lossy(&line));
                        }
                        _ => {}
                    }
                }
            });

            // Once the backend is listening, point the (loading) window at it.
            // The frozen backend cold-starts (unpack + heavy imports), so allow a
            // generous window; on timeout, surface a hint instead of hanging silently.
            std::thread::spawn(move || {
                let ready = wait_for_port(PORT, Duration::from_secs(240));
                if let Some(win) = handle.get_webview_window("main") {
                    if ready {
                        let url = format!("http://127.0.0.1:{PORT}/");
                        if let Ok(parsed) = url.parse() {
                            let _ = win.navigate(parsed);
                        }
                    } else {
                        eprintln!("[desktop] backend did not become ready within 240s");
                        let _ = win.eval(
                            r#"document.body.innerHTML = "<div style='font-family:system-ui;max-width:640px;margin:12vh auto;padding:0 1.5rem;color:#ddd'><h2>The local engine didn't start</h2><p>The bundled backend didn't become ready in time. On macOS this is usually Gatekeeper blocking the app because it isn't signed.</p><p>Open <b>Terminal</b> and run:</p><pre style='background:#000;padding:.75rem;border-radius:6px;overflow:auto'>codesign --force --deep --sign - /Applications/ND3X.app</pre><p>then reopen ND3X.</p></div>";"#,
                        );
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::Destroyed) {
                if let Some(child) = window.state::<Backend>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
                // The backend is a PyInstaller onefile (a bootloader that spawns the
                // real Python process). Killing the child above only reaps the
                // bootloader; the Python process can orphan and keep the port. Make
                // sure it's gone so the engine doesn't linger after the app quits.
                #[cfg(unix)]
                {
                    let _ = std::process::Command::new("pkill")
                        .args(["-f", "nd3x-backend"])
                        .status();
                }
                #[cfg(windows)]
                {
                    let _ = std::process::Command::new("taskkill")
                        .args(["/F", "/IM", "nd3x-backend.exe"])
                        .status();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the ND3X desktop app");
}
