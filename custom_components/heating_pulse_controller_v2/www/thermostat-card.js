/* Use HA's thermostat dial; only its mode colour is overridden by actual action. */
export function thermostatColor(state) {
  if (!state || ["unavailable", "unknown"].includes(state.state)) return "var(--state-unavailable-color, #9e9e9e)";
  if (state.attributes.hvac_action === "heating") return "var(--orange-color, #ff9800)";
  if (state.attributes.hvac_action === "idle") return "var(--blue-color, #2196f3)";
  return "var(--grey-color, #9e9e9e)";
}

export class HeatingPulseThermostatCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._generation = 0;
  }
  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("climate.")) throw new Error("A climate entity is required");
    this._config = {...config};
    const generation = ++this._generation;
    this._ready = this._create(generation);
  }
  async _create(generation) {
    try {
      const helpers = await window.loadCardHelpers();
      const card = helpers.createCardElement({...this._config, type: "thermostat"});
      if (generation !== this._generation) return;
      this._card = card;
      this.shadowRoot.replaceChildren(card);
      if (this._hass) this.hass = this._hass;
    } catch (error) {
      if (generation !== this._generation) return;
      this.shadowRoot.textContent = "Unable to load thermostat: " + error.message;
    }
  }
  set hass(hass) {
    this._hass = hass;
    if (!this._card) return;
    this._card.hass = hass;
    const color = thermostatColor(hass.states[this._config.entity]);
    for (const mode of ["heat", "off", "active", "inactive"]) {
      this._card.style.setProperty(`--state-climate-${mode}-color`, color);
    }
  }
  getCardSize() { return this._card?.getCardSize?.() ?? 5; }
  getGridOptions() { return this._card?.getGridOptions?.() ?? {columns: 6, rows: 4}; }
  static getStubConfig(hass) {
    const entity = Object.values(hass.states).find(s => s.entity_id.startsWith("climate.") && "controller_phase" in s.attributes)?.entity_id;
    return {entity};
  }
}
if (!customElements.get("heating-pulse-thermostat-card")) {
  customElements.define("heating-pulse-thermostat-card", HeatingPulseThermostatCard);
  window.customCards = window.customCards || [];
  window.customCards.push({type: "heating-pulse-thermostat-card", name: "Heating Pulse Thermostat", description: "Orange heating, blue idle, grey off"});
}
