//
//  Config.example.swift  —  TEMPLATE, not compiled.
//
//  Copy this file to HealthTracker/Config.swift and fill in the token.
//  Config.swift is gitignored so the secret never lands in git.
//
//      cp Config.example.swift HealthTracker/Config.swift
//      # then set authToken to the value of the ingest-token secret
//

import Foundation

enum Config {
    // The laptop (`lenovo-agents`), reached over Tailscale. Replaced the Cloud Run
    // URL when the backend moved off GCP — see deploy/README.md.
    //
    // HTTPS is not optional here. iOS App Transport Security blocks cleartext HTTP,
    // so `http://100.97.58.7:8080` fails on the phone while working fine from curl.
    // `tailscale serve` terminates TLS with a real Let's Encrypt cert for this
    // name, which is why the app can talk to a machine in your living room without
    // an ATS exception.
    //
    // Reachable only from inside the tailnet — the phone must have Tailscale on.
    // To roll back to Cloud Run, put the old URL back:
    //   https://health-tracker-ingest-myznjtlyrq-ew.a.run.app
    static let baseURL = URL(string: "https://lenovo-agents.tail68e120.ts.net")!
    static let authToken = "PUT-THE-INGEST-TOKEN-HERE"
}
