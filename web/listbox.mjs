function requireElement(value, label) {
    if (!value || typeof value.addEventListener !== 'function') {
        throw new TypeError(`listbox 缺少 ${label}`);
    }
    return value;
}

function optionList(listbox, selector) {
    return [...listbox.querySelectorAll(selector)].filter(option => (
        !('disabled' in option) || option.disabled !== true
    ));
}

function actionList(panel, selector) {
    return [...panel.querySelectorAll(selector)].filter(action => (
        !('disabled' in action) || action.disabled !== true
    ));
}

export function clampAnchoredLeft(anchorLeft, panelWidth, viewportWidth, margin = 16) {
    const safeMargin = Number.isFinite(margin) && margin >= 0 ? margin : 16;
    const safeAnchor = Number.isFinite(anchorLeft) ? anchorLeft : safeMargin;
    const safePanel = Number.isFinite(panelWidth) && panelWidth >= 0 ? panelWidth : 0;
    const safeViewport = Number.isFinite(viewportWidth) && viewportWidth >= 0
        ? viewportWidth
        : safePanel + safeMargin * 2;
    const maxLeft = Math.max(safeMargin, safeViewport - safePanel - safeMargin);
    return Math.max(safeMargin, Math.min(safeAnchor, maxLeft));
}

export function createListboxController(options = {}) {
    const trigger = requireElement(options.trigger, '触发按钮');
    const panel = requireElement(options.panel, '下拉面板');
    const listbox = requireElement(options.listbox, '选项列表');
    const selector = options.optionSelector || '[role="option"]';
    const actionSelector = options.actionSelector || 'button:not([disabled]), a[href]';
    const hiddenClass = options.hiddenClass || 'hidden';
    const documentRef = options.documentRef || trigger.ownerDocument;
    const listboxId = listbox.id || `${trigger.id || 'listbox'}-options`;
    listbox.id = listboxId;
    listbox.setAttribute('role', 'listbox');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-controls', listboxId);

    let activeIndex = -1;
    let destroyed = false;

    function isOpen() {
        return !panel.classList.contains(hiddenClass);
    }

    function selectedIndex(items) {
        const configured = items.findIndex(option => (
            typeof options.isSelected === 'function'
                ? options.isSelected(option)
                : option.classList.contains('active')
        ));
        return configured >= 0 ? configured : 0;
    }

    function setActive(items, index, focus = true) {
        if (items.length === 0) {
            activeIndex = -1;
            listbox.removeAttribute('aria-activedescendant');
            return null;
        }
        activeIndex = Math.max(0, Math.min(index, items.length - 1));
        const option = items[activeIndex];
        listbox.setAttribute('aria-activedescendant', option.id);
        if (focus && typeof option.focus === 'function') option.focus();
        return option;
    }

    function refresh() {
        const items = optionList(listbox, selector);
        items.forEach((option, index) => {
            option.id = option.id || `${listboxId}-option-${index + 1}`;
            option.setAttribute('role', 'option');
            option.setAttribute('tabindex', '-1');
            const selected = typeof options.isSelected === 'function'
                ? options.isSelected(option)
                : option.classList.contains('active');
            option.setAttribute('aria-selected', String(Boolean(selected)));
        });
        activeIndex = items.length ? selectedIndex(items) : -1;
        if (isOpen() && items[activeIndex]) {
            listbox.setAttribute('aria-activedescendant', items[activeIndex].id);
        } else {
            listbox.removeAttribute('aria-activedescendant');
        }
        return items;
    }

    function close({ restoreFocus = false } = {}) {
        panel.classList.add(hiddenClass);
        trigger.setAttribute('aria-expanded', 'false');
        listbox.removeAttribute('aria-activedescendant');
        if (restoreFocus && typeof trigger.focus === 'function') trigger.focus();
    }

    function canOpen() {
        return typeof options.canOpen !== 'function' || options.canOpen();
    }

    function actions() {
        return actionList(panel, actionSelector);
    }

    function open({ focus = 'selected' } = {}) {
        if (!canOpen()) {
            if (typeof options.onBlocked === 'function') options.onBlocked();
            return false;
        }
        if (typeof options.beforeOpen === 'function') options.beforeOpen();
        panel.classList.remove(hiddenClass);
        trigger.setAttribute('aria-expanded', 'true');
        if (typeof options.position === 'function') options.position(panel, trigger);
        const items = refresh();
        if (focus === 'first') setActive(items, 0);
        else if (focus === 'last') setActive(items, items.length - 1);
        else if (focus === 'selected') setActive(items, selectedIndex(items));
        return true;
    }

    function toggle() {
        if (isOpen()) {
            close({ restoreFocus: true });
            return false;
        }
        return open({ focus: false });
    }

    function choose(option) {
        if (!option || !option.matches(selector)) return false;
        close({ restoreFocus: true });
        if (typeof options.onSelect === 'function') options.onSelect(option);
        return true;
    }

    function onTriggerClick(event) {
        event.stopPropagation();
        toggle();
    }

    function onTriggerKeydown(event) {
        let focus = null;
        if (event.key === 'ArrowDown') focus = 'first';
        else if (event.key === 'ArrowUp') focus = 'last';
        else if (event.key === 'Home') focus = 'first';
        else if (event.key === 'End') focus = 'last';
        else if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            if (isOpen()) close({ restoreFocus: true });
            else open({ focus: 'selected' });
            return;
        } else if (event.key === 'Escape' && isOpen()) {
            event.preventDefault();
            close({ restoreFocus: true });
            return;
        } else if (event.key === 'Tab' && isOpen()) {
            if (event.shiftKey) {
                close({ restoreFocus: false });
                return;
            }
            event.preventDefault();
            const items = refresh();
            if (items.length > 0) setActive(items, selectedIndex(items));
            else {
                const availableActions = actions();
                if (availableActions[0] && typeof availableActions[0].focus === 'function') {
                    availableActions[0].focus();
                } else close({ restoreFocus: false });
            }
            return;
        }
        if (focus) {
            event.preventDefault();
            if (!isOpen()) open({ focus });
            else {
                const items = refresh();
                setActive(items, focus === 'first' ? 0 : items.length - 1);
            }
        }
    }

    function onListboxKeydown(event) {
        const items = refresh();
        const focused = documentRef && documentRef.activeElement;
        const focusedIndex = items.indexOf(focused);
        if (focusedIndex >= 0) activeIndex = focusedIndex;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (items.length === 0) return;
            const delta = event.key === 'ArrowDown' ? 1 : -1;
            const base = activeIndex >= 0 ? activeIndex : selectedIndex(items);
            setActive(items, (base + delta + items.length) % items.length);
        } else if (event.key === 'Home' || event.key === 'End') {
            event.preventDefault();
            setActive(items, event.key === 'Home' ? 0 : items.length - 1);
        } else if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            choose(items[activeIndex] || focused);
        } else if (event.key === 'Escape') {
            event.preventDefault();
            close({ restoreFocus: true });
        } else if (event.key === 'Tab') {
            event.preventDefault();
            if (event.shiftKey) close({ restoreFocus: true });
            else {
                const availableActions = actions();
                if (availableActions[0] && typeof availableActions[0].focus === 'function') {
                    availableActions[0].focus();
                } else close({ restoreFocus: true });
            }
        }
    }

    function onPanelKeydown(event) {
        if (!isOpen() || (event.target && listbox.contains(event.target))) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            close({ restoreFocus: true });
            return;
        }
        if (event.key !== 'Tab' || !event.shiftKey) return;
        const availableActions = actions();
        if (availableActions[0] !== event.target) return;
        event.preventDefault();
        const items = refresh();
        if (items.length > 0) setActive(items, activeIndex >= 0 ? activeIndex : selectedIndex(items));
        else close({ restoreFocus: true });
    }

    function onDocumentFocusIn(event) {
        if (!isOpen() || event.target === trigger || panel.contains(event.target)) return;
        close({ restoreFocus: false });
    }

    function onListboxClick(event) {
        const option = event.target && typeof event.target.closest === 'function'
            ? event.target.closest(selector)
            : null;
        if (option && listbox.contains(option)) choose(option);
    }

    trigger.addEventListener('click', onTriggerClick);
    trigger.addEventListener('keydown', onTriggerKeydown);
    listbox.addEventListener('keydown', onListboxKeydown);
    listbox.addEventListener('click', onListboxClick);
    panel.addEventListener('keydown', onPanelKeydown);
    if (documentRef && typeof documentRef.addEventListener === 'function') {
        documentRef.addEventListener('focusin', onDocumentFocusIn);
    }
    close();
    refresh();

    return Object.freeze({
        open,
        close,
        toggle,
        refresh,
        isOpen,
        destroy() {
            if (destroyed) return;
            destroyed = true;
            trigger.removeEventListener('click', onTriggerClick);
            trigger.removeEventListener('keydown', onTriggerKeydown);
            listbox.removeEventListener('keydown', onListboxKeydown);
            listbox.removeEventListener('click', onListboxClick);
            panel.removeEventListener('keydown', onPanelKeydown);
            if (documentRef && typeof documentRef.removeEventListener === 'function') {
                documentRef.removeEventListener('focusin', onDocumentFocusIn);
            }
            close();
        },
    });
}
