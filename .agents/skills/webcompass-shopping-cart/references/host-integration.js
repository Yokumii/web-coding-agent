// Host-first integration and representative composition for WebCompass Edit.
function wireShoppingCart(host) {
  // items and add controls belong to the current catalog; do not introduce a second product list.
  const cart = mountShoppingCart({items: host.state.items, container: host.container, addRoot: host.addRoot, addSelector: host.addSelector || '[data-add-to-cart]', getItemId: host.getItemId, formatMoney: host.formatMoney, initialQuantities: host.state.basket, classes: host.classes, labels: host.labels});
  host.checkoutButton?.addEventListener('click', () => host.checkout(cart.snapshot()));
  return cart;
}
