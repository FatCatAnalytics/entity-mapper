// Entity Mapper desktop shell.
//
// The window is only a frame: it starts the Python matching engine (a local web server bound to
// 127.0.0.1 on a free port), waits until it answers, then shows the engine's own pages. The same
// pages run in a plain browser on macOS via `python run.py`, so nothing in the UI depends on Tauri.
// The engine is stopped when the window closes, and stops itself if this process disappears.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent};

const START_TIMEOUT: Duration = Duration::from_secs(180);

struct Engine(Mutex<Option<Child>>);

fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port())
}

fn healthy(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    if stream
        .write_all(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut head = [0u8; 32];
    let n = stream.read(&mut head).unwrap_or(0);
    String::from_utf8_lossy(&head[..n]).contains(" 200")
}

/// The packaged engine from the app's resources; in a debug build, the engine run from source.
fn engine_command(app: &AppHandle) -> Result<Command, String> {
    let exe = if cfg!(windows) { "engine.exe" } else { "engine" };
    if let Ok(dir) = app.path().resource_dir() {
        let packaged = dir.join("engine").join(exe);
        if packaged.exists() {
            return Ok(Command::new(packaged));
        }
    }
    #[cfg(debug_assertions)]
    {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("project folder not found")?
            .to_path_buf();
        let venv = if cfg!(windows) {
            root.join(".venv").join("Scripts").join("python.exe")
        } else {
            root.join(".venv").join("bin").join("python")
        };
        let python = if venv.exists() {
            venv
        } else {
            std::path::PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
        };
        let mut cmd = Command::new(python);
        cmd.arg(root.join("engine_main.py")).current_dir(&root);
        return Ok(cmd);
    }
    #[allow(unreachable_code)]
    Err("The matching engine is missing from this installation. Reinstall Entity Mapper.".into())
}

fn start_engine(app: &AppHandle, port: u16) -> Result<Child, String> {
    let mut cmd = engine_command(app)?;
    cmd.arg("--port")
        .arg(port.to_string())
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .stdin(Stdio::null());
    if !cfg!(debug_assertions) {
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd.spawn()
        .map_err(|e| format!("The matching engine could not be started: {e}"))
}

/// Wait for the engine, then show it; on failure, explain on the start page.
fn wait_then_show(app: AppHandle, port: u16) {
    let began = Instant::now();
    let problem = loop {
        if healthy(port) {
            break None;
        }
        let exited = {
            let state = app.state::<Engine>();
            let mut guard = state.0.lock().unwrap();
            match guard.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(status))) => Some(status),
                _ => None,
            }
        };
        if let Some(status) = exited {
            break Some(if status.code() == Some(3) {
                "The engine stopped while opening the workspace. Is another copy of Entity Mapper, or \
                 `python run.py`, already running on this computer?"
                    .to_string()
            } else {
                format!("The engine stopped while starting ({status}).")
            });
        }
        if began.elapsed() > START_TIMEOUT {
            break Some("The engine did not answer within 3 minutes.".to_string());
        }
        std::thread::sleep(Duration::from_millis(250));
    };

    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    match problem {
        None => {
            let url = format!("http://127.0.0.1:{port}/");
            let _ = window.navigate(url.parse().expect("local url"));
        }
        Some(message) => {
            // Give the start page a moment to load before calling into it.
            let shown = began.elapsed();
            if shown < Duration::from_secs(2) {
                std::thread::sleep(Duration::from_secs(2) - shown);
            }
            let js = format!(
                "window.engineFailed && window.engineFailed({})",
                serde_json::to_string(&message).unwrap_or_else(|_| "\"\"".into())
            );
            let _ = window.eval(&js);
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // A second launch brings the first window forward: one engine, one workspace.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(Engine(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let port = free_port()?;
            match start_engine(&handle, port) {
                Ok(child) => *handle.state::<Engine>().0.lock().unwrap() = Some(child),
                Err(message) => eprintln!("{message}"),
            }
            // Runs even if the engine failed to spawn: the loop then reports the failure.
            std::thread::spawn(move || {
                if handle.state::<Engine>().0.lock().unwrap().is_none() {
                    std::thread::sleep(Duration::from_secs(2));
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.eval(
                            "window.engineFailed && window.engineFailed('The matching engine could not be started.')",
                        );
                    }
                    return;
                }
                wait_then_show(handle, port);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Entity Mapper");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<Engine>().0.lock().unwrap().take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}
