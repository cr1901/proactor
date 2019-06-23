extern crate argparse;

use std::time::Duration;
use std::thread::sleep;

use argparse::{ArgumentParser, StoreTrue, Store};

fn main() {
    let mut n : u8 = 4;
    let mut infinite = false;
    let mut dur = 1000;

    {
        let mut ap = ArgumentParser::new();
        ap.set_description("Echo to the console.");
        ap.refer(&mut n)
            .add_option(&["-n"], Store,
            "Number of Lines to Echo");
        ap.refer(&mut infinite)
            .add_option(&["-i"], StoreTrue,
            "Echo infinitely. Overrides n");
        ap.refer(&mut dur)
            .add_option(&["-d"], Store,
            "Interval in milliseconds between messages.");
        ap.parse_args_or_exit();
    }


    if infinite {
        let mut j : u8 = 0;
        loop {
            println!("Infinite Loop, Iteration {:?}", j);
            sleep(Duration::from_millis(dur));
            j = j.wrapping_add(1);
        }
    } else {
        for j in 0..n {
            println!("Finite Loop, Iteration {:?}", j);
            sleep(Duration::from_millis(dur));
        }
    }
}
