"""
Tema Dark Moderno com paleta profissional.
"""

DARK_THEME_QSS = """
QMainWindow, QWidget {
    background-color: #18181b;
    color: #f4f4f5;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
    font-size: 13px;
}

/* Painéis e Caixas de Grupo */
QGroupBox {
    border: 1px solid #3f3f46;
    border-radius: 6px;
    margin-top: 10px;
    font-weight: bold;
    padding-top: 15px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    color: #38bdf8;
    padding: 0 4px;
}

/* Botões */
QPushButton {
    background-color: #27272a;
    color: #fafafa;
    border: 1px solid #3f3f46;
    border-radius: 5px;
    padding: 6px 11px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #3f3f46;
    border-color: #71717a;
}
QPushButton:pressed {
    background-color: #18181b;
}
QPushButton:disabled {
    background-color: #27272a;
    color: #71717a;
    border-color: #27272a;
}

/* Botão de Ação Primária (Escanear) */
QPushButton#primaryButton {
    background-color: #0284c7;
    color: #ffffff;
    border: none;
    font-weight: bold;
}
QPushButton#primaryButton:hover {
    background-color: #0369a1;
}
QPushButton#primaryButton:disabled {
    background-color: #3f3f46;
    color: #71717a;
    border: 1px solid #27272a;
}

/* Botão de Parar */
QPushButton#stopButton {
    background-color: #3f3f46;
    color: #f4f4f5;
    border: 1px solid #52525b;
    font-weight: bold;
}
QPushButton#stopButton:hover {
    background-color: #52525b;
}
QPushButton#stopButton:disabled {
    background-color: #27272a;
    color: #52525b;
    border: 1px solid #27272a;
}

/* Botão de Exclusão (Lixeira) */
QPushButton#dangerButton {
    background-color: #b91c1c;
    color: #ffffff;
    border: none;
    font-weight: bold;
}
QPushButton#dangerButton:hover {
    background-color: #991b1b;
}
QPushButton#dangerButton:disabled {
    background-color: #27272a;
    color: #52525b;
    border: 1px solid #27272a;
}

/* Botão Circular de Ajuda (?) - Inspirado na Identidade do RadarEleitoral */
QPushButton#helpButton {
    background-color: #202024;
    border: 1px solid #3f3f46;
    border-radius: 14px;
    color: #f4f4f5;
    font-size: 13px;
    font-weight: bold;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
    padding: 0px;
    text-align: center;
}
QPushButton#helpButton:hover {
    background-color: #27272a;
    border-color: #38bdf8;
    color: #38bdf8;
}
QPushButton#helpButton:pressed {
    background-color: #18181b;
    border-color: #0284c7;
    color: #0284c7;
}
QPushButton#helpButton::menu-indicator {
    width: 0px;
    height: 0px;
    image: none;
}

/* Botão Quadradinho com Checkmark da Pasta de Referência */
QPushButton#refSquare {
    width: 18px;
    height: 18px;
    min-width: 18px;
    max-width: 18px;
    min-height: 18px;
    max-height: 18px;
    border: 1px solid #52525b;
    border-radius: 3px;
    background-color: #202024;
    color: transparent;
    font-size: 12px;
    font-weight: 900;
    padding: 0px;
    text-align: center;
}
QPushButton#refSquare:hover {
    border-color: #38bdf8;
}
QPushButton#refSquare:checked {
    background-color: #38bdf8;
    border-color: #38bdf8;
    color: #0f172a;
}

/* Tabelas e Árvores */
QTreeWidget, QTableWidget {
    background-color: #27272a;
    alternate-background-color: #202023;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    gridline-color: #3f3f46;
    color: #f4f4f5;
    outline: none;
}
QTreeWidget::item, QTableWidget::item {
    padding: 4px;
    border-bottom: 1px solid #2e2e33;
}
QTreeWidget::item:selected, QTableWidget::item:selected {
    background-color: #0369a1;
    color: #ffffff;
}

/* Cabeçalhos de Tabela */
QHeaderView {
    background-color: transparent;
    border: none;
}
QHeaderView::section {
    background-color: #1f1f23;
    color: #a1a1aa;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid #3f3f46;
    border-bottom: 1px solid #3f3f46;
    font-weight: bold;
}
QHeaderView::section:first {
    border-top-left-radius: 5px;
}
QHeaderView::section:last {
    border-top-right-radius: 5px;
    border-right: none;
}
QHeaderView::section:hover {
    background-color: #27272a;
    color: #38bdf8;
}

/* Sliders */
QSlider::groove:horizontal {
    border: 1px solid #3f3f46;
    height: 6px;
    background: #27272a;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: #0284c7;
    border-radius: 3px;
}
QSlider::sub-page:horizontal:disabled {
    background: #27272a;
}
QSlider::handle:horizontal {
    background: #38bdf8;
    border: 1px solid #0284c7;
    width: 16px;
    margin-top: -5px;
    margin-bottom: -5px;
    border-radius: 8px;
}
QSlider::handle:horizontal:disabled {
    background: #52525b;
    border: 1px solid #3f3f46;
}

/* Barra de Progresso Geral e Estágios */
QProgressBar {
    border: 1px solid #3f3f46;
    border-radius: 4px;
    text-align: center;
    background-color: #27272a;
    color: #fafafa;
    font-weight: bold;
    min-height: 18px;
    max-height: 18px;
}
QProgressBar::chunk {
    background-color: #0284c7;
    border-radius: 3px;
}

QProgressBar#progCurrent, QProgressBar#progAll {
    border: 1px solid #3f3f46;
    border-radius: 4px;
    background-color: #18181b;
    height: 8px;
    min-height: 8px;
    max-height: 8px;
}

QProgressBar#progCurrent::chunk, QProgressBar#progAll::chunk {
    background-color: #38bdf8;
    border-radius: 3px;
}

/* Container da Barra Lateral (Divisão Contínua e Elegante de Cima a Baixo) */
QWidget#sidebarContainer {
    background-color: #151518;
    border-right: 1px solid #2e2e33;
}

/* Barra Lateral de Modos (Caixa Única Envolvendo os 3 Modos) */
QListWidget#sidebarModes {
    background-color: #202024;
    border: 1px solid #2e2e33;
    border-radius: 6px;
    padding: 2px;
    outline: none;
    font-size: 13px;
    font-weight: 500;
}
QListWidget#sidebarModes:disabled {
    background-color: #18181b;
    border: 1px solid #222226;
}
QListWidget#sidebarModes:focus {
    border: 1px solid #2e2e33;
    outline: none;
}
QListWidget#sidebarModes::item {
    padding: 9px 12px;
    background-color: transparent;
    border: none;
    border-radius: 4px;
    color: #e4e4e7;
    margin: 1px;
}
QListWidget#sidebarModes::item:hover {
    background-color: #27272a;
    color: #ffffff;
}
QListWidget#sidebarModes::item:selected {
    background-color: #2c2c34;
    color: #38bdf8;
    border: none;
    font-weight: bold;
}
QListWidget#sidebarModes::item:disabled {
    color: #52525b;
}
QListWidget#sidebarModes::item:selected:disabled {
    background-color: #1c1c20;
    color: #71717a;
    border: none;
    font-weight: 500;
}
QListWidget#sidebarModes::item:focus {
    outline: none;
}

/* Menus de Seleção e Contexto */
QMenu {
    background-color: #202024;
    color: #f4f4f5;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 18px;
    border-radius: 4px;
    font-size: 12px;
}
QMenu::item:selected {
    background-color: #0284c7;
    color: #ffffff;
}
QMenu::separator {
    height: 1px;
    background-color: #3f3f46;
    margin: 4px 6px;
}

/* Divisores de Splitter (Vertical e Horizontal) */
QSplitter::handle {
    background-color: transparent;
}
QSplitter::handle:hover {
    background-color: #38bdf8;
}
QSplitter::handle:horizontal {
    width: 4px;
}
QSplitter::handle:vertical {
    height: 4px;
}
"""
