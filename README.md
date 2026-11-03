# DupeClean ⚡

**Deduplicação de Arquivos por GPU**

Deduplicador de arquivos, fotos e vídeos de alta performance, desenhado e otimizado exclusivamente para placas **NVIDIA GeForce RTX (Séries 30, 40 e 50)** com arquiteturas Ampere, Ada Lovelace e Blackwell.

---

## 🚀 Diferenciais de Engenharia

* **Sem gargalo de CPU:** Todo o processamento de imagens e comparação vetorial ocorre diretamente na **VRAM** e nos **Tensor Cores** da GPU via PyTorch CUDA.
* **Decodificação Paralela e NVDEC:** Carregamento multithread de imagens em alta velocidade e extração temporal de frames utilizando os decodificadores dedicados da NVIDIA via FFmpeg Hardware Acceleration (`-hwaccel cuda`).
* **Proteção Estrita de Pastas de Referência (`Ref`):** Marque qualquer pasta mestre/backup como **Referência**. O DupeClean compara arquivos novos contra ela, mas **nunca sugere ou marca arquivos da pasta de referência para exclusão**.
* **Ergonomia Inspirada no Krokiet:**
  * Dark Mode nativo com design limpo (KISS).
  * Suporte a arrastar e soltar (Drag & Drop) pastas do Windows Explorer.
  * Slider de similaridade (70% a 100%) para fotos e vídeos.
  * Envio seguro de arquivos duplicados para a **Lixeira do Windows** (`Send2Trash`).

---

## 🛠️ Como Executar

### Opção 1: Atalho Rápido
Dê dois cliques no arquivo:
```cmd
run.bat
```

### Opção 2: Pelo Terminal
```powershell
cd W:\Projetos\Pessoal\DupeClean
.\.venv\Scripts\activate
python main.py
```

---

## 💻 Requisitos de Hardware
* **GPU:** NVIDIA GeForce RTX Série 30, 40 ou 50 (Compute Capability ≥ 8.6).
* **Driver NVIDIA:** 550+ (com suporte a CUDA 12.4+ / 12.8+).
* **Sistema:** Windows 10/11 64-bit.
