//
//  DiskCache.swift
//  HealthTracker
//
//  A tiny raw-bytes cache so the last good server response survives a relaunch.
//  Caches the undecoded JSON (not the decoded struct) so every response type gets
//  offline persistence for free, with no Encodable conformance to maintain and no
//  risk of the cache drifting from whatever CodingKeys the model actually uses.
//

import Foundation

enum DiskCache {
    private static let dir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let url = base.appending(path: "HealthTrackerCache", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }()

    static func save(_ data: Data, as name: String) {
        try? data.write(to: dir.appending(path: "\(name).json"), options: .atomic)
    }

    static func loadData(as name: String) -> Data? {
        try? Data(contentsOf: dir.appending(path: "\(name).json"))
    }

    static func load<T: Decodable>(_ type: T.Type, as name: String) -> T? {
        guard let data = loadData(as: name) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }
}
