// Host-first integration and representative composition for WebCompass Edit.
function wireAuthentication(host) {
  // The adapter may simulate authentication, but credentials are request-only and are never persisted.
  return mountAuthentication({container: host.container, authenticate: (credentials, o) => host.auth.authenticate(credentials, o), restoreSession: o => host.auth.currentSession(o), signOut: o => host.auth.signOut(o), onSession: user => { host.state.session = user; host.renderHeader(user); host.renderProtectedRoutes(user); }, allowRegistration: !!host.allowRegistration});
}
