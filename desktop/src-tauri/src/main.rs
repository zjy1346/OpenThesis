#![cfg_attr(
    all(target_os = "windows", not(debug_assertions)),
    windows_subsystem = "windows"
)]

fn main() {
    if std::env::args().any(|argument| argument == "--model-gateway") {
        std::process::exit(openthesis_desktop_lib::run_model_gateway_stdio());
    }
    openthesis_desktop_lib::run();
}
