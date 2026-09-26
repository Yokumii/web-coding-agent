// Host-first integration and representative composition for WebCompass Edit.
function wireNotificationCenter(host) {
  const center = mountNotificationCenter({container: host.container, notifications: host.state.notifications, allowDelete: !!host.allowDelete, onAction: n => host.openTarget(n), onChange: (entries, unread) => host.saveNotifications(entries, unread), onCreateRow: host.onCreateRow});
  host.eventBus?.on('notification', n => center.push(n));
  return center;
}
