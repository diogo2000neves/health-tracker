//
//  ContentView.swift
//  HealthTracker
//
//  The app shell: a three-tab layout over one shared "today" fetch (Hoje and
//  Nutrientes read the same payload) plus a separate history fetch for Tendências.
//  Everything reloads on launch, on pull-to-refresh, and whenever the app returns
//  to the foreground, so a meal logged while you were away is there when you return.
//

import SwiftUI

// MARK: - Stores

@MainActor
@Observable
final class TodayStore {
    var response: TodayResponse?
    var errorMessage: String?
    var isLoading = false

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            response = try await APIClient.shared.today()
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

@MainActor
@Observable
final class TrendsStore {
    var response: DailyResponse?
    var errorMessage: String?
    var isLoading = false

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let today = Date()
            let from = Calendar.current.date(byAdding: .day, value: -90, to: today)!
            response = try await APIClient.shared.daily(from: Self.iso(from), to: Self.iso(today))
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private static func iso(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}

// MARK: - Root

struct RootView: View {
    @State private var today = TodayStore()
    @State private var trends = TrendsStore()
    @State private var showProfile = false
    @State private var selection = 0
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        TabView(selection: $selection) {
            Tab("Hoje", systemImage: "flame.fill", value: 0) {
                TodayView(store: today, showProfile: $showProfile)
            }
            Tab("Nutrientes", systemImage: "leaf.fill", value: 1) {
                NutrientsView(store: today)
            }
            Tab("Tendências", systemImage: "chart.xyaxis.line", value: 2) {
                TrendsView(store: trends, today: today)
            }
        }
        .task {
            await today.load()
            await trends.load()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await today.load() }
                Task { await trends.load() }
            }
        }
        .sheet(isPresented: $showProfile) {
            ProfileView(store: today)
        }
        .onAppear {
            #if DEBUG
            // Dev affordance: jump straight to a tab when running the sample build
            // (SIMCTL_CHILD_START_TAB=1). No effect in release.
            if let raw = ProcessInfo.processInfo.environment["START_TAB"],
               let value = Int(raw) { selection = value }
            #endif
        }
    }
}

// MARK: - Shared small views

/// A centred loading / error placeholder shared by the tabs.
struct LoadingOrError: View {
    let isLoading: Bool
    let error: String?
    let retry: () -> Void

    var body: some View {
        if isLoading {
            ProgressView("A carregar…")
        } else if let error {
            ContentUnavailableView {
                Label("Não deu para carregar", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error)
            } actions: {
                Button("Tentar de novo", action: retry)
                    .buttonStyle(.borderedProminent)
            }
        }
    }
}

/// The long-form pt-PT date, e.g. "sexta-feira, 18 de julho".
func prettyDate(_ iso: String) -> String {
    let parser = DateFormatter()
    parser.calendar = Calendar(identifier: .gregorian)
    parser.locale = Locale(identifier: "en_US_POSIX")
    parser.dateFormat = "yyyy-MM-dd"
    guard let date = parser.date(from: iso) else { return iso }
    let out = DateFormatter()
    out.locale = Locale(identifier: "pt_PT")
    out.dateFormat = "EEEE, d 'de' MMMM"
    let text = out.string(from: date)
    // Capitalise only the first letter (the weekday); "de julho" stays lowercase,
    // as Portuguese wants — .capitalized would wrongly give "De Julho".
    return text.prefix(1).uppercased() + text.dropFirst()
}

#Preview {
    RootView()
}
