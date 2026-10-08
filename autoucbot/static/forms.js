// Local progressive enhancement; the full form is usable without JavaScript.
(() => {
  'use strict';
  const sku = document.getElementById('product-sku');
  if (!sku) return;
  const update = () => {
    const item = sku.selectedOptions[0];
    if (!item) return;
    const service = document.getElementById('product-service');
    const units = document.querySelector('[name="sku_uc"]');
    const field = document.querySelector('[name="uid_field"]');
    if (service) service.value = item.dataset.service || 'uc';
    if (units && Number(item.dataset.units) > 0) units.value = item.dataset.units;
    // Multiple dynamic fields: owner must choose, not reuse another service's field.
    if (field) field.value = item.dataset.field || '';
  };
  sku.addEventListener('change', update);
  const id = document.querySelector('[name="id"]');
  if (id && id.value === '0') update();
})();
