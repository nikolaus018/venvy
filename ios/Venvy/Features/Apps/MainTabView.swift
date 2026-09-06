import SwiftUI

struct MainTabView: View {
    @EnvironmentObject private var session: VenvySession
    @State private var selection: Tab = .apps

    enum Tab: Hashable { case apps, system, account }

    var body: some View {
        TabView(selection: $selection) {
            AppListView()
                .tabItem { Label("Apps", systemImage: "square.stack.3d.up.fill") }
                .tag(Tab.apps)

            if session.isAdmin {
                SystemView()
                    .tabItem { Label("System", systemImage: "chart.bar.xaxis") }
                    .tag(Tab.system)
            }

            AccountView()
                .tabItem { Label("Account", systemImage: "person.crop.circle") }
                .tag(Tab.account)
        }
        .task { await session.loadServerIPv4() }
    }
}
