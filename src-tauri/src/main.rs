#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if arguments
        .first()
        .is_some_and(|value| value == "--verify-engine")
    {
        if arguments.len() != 3 || arguments[1] != "--data-dir" {
            eprintln!("Usage: q22-flight-lab --verify-engine --data-dir DIRECTORY");
            std::process::exit(2);
        }
        match q22_flight_lab::verify_bundled_engine(std::path::PathBuf::from(&arguments[2])) {
            Ok(result) => println!("{result}"),
            Err(message) => {
                eprintln!("Native engine verification failed: {message}");
                std::process::exit(1);
            }
        }
        return;
    }
    q22_flight_lab::run();
}
