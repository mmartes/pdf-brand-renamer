# PDF Brand Renamer

Extracts the vendor/brand name from Tableau-generated PDF reports and renames them using a customizable filename template.

## Building the Windows .exe via GitHub

1. Create a new repo on GitHub (public or private)
2. Push this entire folder to it:
   ```
   cd pdf-brand-renamer
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```
3. Go to your repo on GitHub → **Actions** tab
4. The "Build Windows EXE" workflow runs automatically on push
5. Once it finishes (about 2 minutes), click the completed run
6. Download **PDF-Brand-Renamer-Windows** from the Artifacts section

That zip contains `PDF Brand Renamer.exe` — a standalone Windows executable that works without Python installed.

## Running with Python (Mac/Linux/Windows)

```
pip install pdfplumber
python pdf_brand_renamer.py
```
