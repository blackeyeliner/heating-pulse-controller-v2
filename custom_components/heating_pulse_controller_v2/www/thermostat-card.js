/* Preserve HA thermostat controls; distinguish autonomous frost protection visually. */
export function isFrostHeating(state) {
  return state?.state === "off" && state.attributes.frost_protection_active === true && state.attributes.hvac_action === "heating";
}
export function thermostatColor(state) {
  if (!state || ["unavailable", "unknown"].includes(state.state)) return "var(--state-unavailable-color, #9e9e9e)";
  if (isFrostHeating(state)) return "var(--red-color, #f44336)";
  if (state.attributes.hvac_action === "heating") return "var(--deep-orange-color, #ff6f22)";
  if (state.attributes.hvac_action === "idle") return "var(--blue-color, #2196f3)";
  return "var(--grey-color, #9e9e9e)";
}

// HA installs its scoped custom-element registry during frontend bootstrap.
// Wait before resolving HTMLElement or defining our card, so both use that registry.
await customElements.whenDefined("home-assistant");

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
    const state = hass.states[this._config.entity];
    const frost = isFrostHeating(state);
    // Keep the native mode features Off during autonomous heating.
    // The real HA entity retains its accurate heating action; no service is sent.
    this._card.hass = frost ? {...hass, states: {...hass.states,
      [this._config.entity]: {...state, attributes: {...state.attributes, hvac_action: "off"}}
    }} : hass;
    const color = thermostatColor(state);
    for (const mode of ["heat", "off", "active", "inactive"]) {
      this._card.style.setProperty(`--state-climate-${mode}-color`, frost ? "var(--grey-color, #9e9e9e)" : color);
    }
    const generation = this._presentationGeneration = (this._presentationGeneration || 0) + 1;
    this._presentationReady = this._updateProtectionLabel(frost, generation);
  }
  async _updateProtectionLabel(frost, generation) {
    const card = this._card;
    await card.updateComplete;
    if (card !== this._card || generation !== this._presentationGeneration) return;
    const control = card.shadowRoot?.querySelector("ha-state-control-climate-temperature");
    if (!control) return;
    await control.updateComplete;
    if (card !== this._card || generation !== this._presentationGeneration) return;
    // Render the dial as Heating only during its synchronous render call.
    // Keep the native reactive state untouched: mode features, event handlers
    // and the real entity still see Off. Parent updates cannot overwrite this
    // presentation because it is applied at render time, not to stateObj.
    if (!control._pulseOriginalRender && typeof control.render === "function") {
      control._pulseOriginalRender = control.render;
      control.render = function() {
        if (!this._pulseFrostHeating) return this._pulseOriginalRender();
        const descriptor = Object.getOwnPropertyDescriptor(this, "stateObj");
        const state = this.stateObj;
        Object.defineProperty(this, "stateObj", {configurable: true, value: {
          ...state, state: "heat", attributes: {...state.attributes, hvac_action: "heating"}
        }});
        try { return this._pulseOriginalRender(); }
        finally {
          if (descriptor) Object.defineProperty(this, "stateObj", descriptor);
          else delete this.stateObj;
        }
      };
    }
    // Scoped to this native control instance; preserve the native label template
    // for all ordinary states and never replace Lit-owned DOM markers.
    if (!control._pulseOriginalRenderLabel && typeof control._renderLabel === "function") {
      control._pulseOriginalRenderLabel = control._renderLabel;
      control._renderLabel = function() {
        if (!this._pulseFrostHeating) return this._pulseOriginalRenderLabel();
        const label = document.createElement("p");
        label.className = "label";
        label.textContent = "Antifreeze";
        return label;
      };
    }
    if (control._pulseFrostHeating !== frost) {
      control._pulseFrostHeating = frost;
      control.requestUpdate();
    }
    const color = "var(--red-color, #f44336)";
    for (const mode of ["heat", "off", "active", "inactive"]) {
      control.style?.setProperty(`--state-climate-${mode}-color`, frost ? color : "");
    }
    // Native Heating styles already colour the status, current-room temperature
    // and active arc. Preserve the target digits and inactive track colours.
    control.shadowRoot?.querySelector(".info")?.style.setProperty("color", "");
    control.shadowRoot?.querySelector("ha-control-circular-slider")?.style.setProperty("--disabled-color", "");
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
