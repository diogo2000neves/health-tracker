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
    static let baseURL = URL(string: "https://health-tracker-ingest-myznjtlyrq-ew.a.run.app")!
    static let authToken = "PUT-THE-INGEST-TOKEN-HERE"
}
