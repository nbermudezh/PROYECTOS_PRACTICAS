from flask import Flask, request, jsonify, render_template, send_file
import pdfplumber
import spacy
import re
import os
from docxtpl import DocxTemplate
from tempfile import NamedTemporaryFile
from datetime import datetime

app = Flask(__name__)

nlp = spacy.load("es_core_news_lg")

# ---------------- LIMPIEZA ----------------

def normalizar_parrafos(texto):

    if not texto:
        return texto

    marcador = "<<<PARRAFO>>>"

    texto = re.sub(r'\n{2,}', marcador, texto)
    texto = re.sub(r'-\s+', '', texto)
    texto = re.sub(r'\n', ' ', texto)
    texto = re.sub(r'\s{2,}', ' ', texto).strip()
    texto = texto.replace(marcador, '\n\n')

    return texto


# ---------------- EXTRAER TEXTO ----------------

def extraer_texto(pdf_path):

    texto = ""

    patrones_ignorados = [

        r"Cra\.?\s*\d+\s*(No\.?|N°|n°)?\s*[\dA-Za-z\-]+",
        r"tel[:\s\.]*\(?57\s*601\)?",
        r"\d{3}\s*\d{4}",
        r"Bogotá,\s*Colombia",
        r"lasalle\.edu\.co",
        r"www\.lasalle\.edu\.co",
        r"UNIVERSIDAD\s+DE\s+LA\s+SALLE",
        r"La\s+Salle\s+Educación\s+Superior",
        r"\b\d+\s+En\s+los\s+casos\s+que\s+aplique\.?"

    ]

    regex_ignorados = re.compile("|".join(patrones_ignorados), re.IGNORECASE)

    with pdfplumber.open(pdf_path) as pdf:

        for pagina in pdf.pages:

            contenido = pagina.extract_text()

            if contenido:

                lineas_limpias = []

                for linea in contenido.split("\n"):

                    if not regex_ignorados.search(linea):

                        lineas_limpias.append(linea)

                texto += "\n".join(lineas_limpias) + "\n"

    return texto


# ---------------- DETECTAR MÍNIMA CUANTÍA ----------------

def es_minima_cuantia(texto):

    return (

        "ORDEN DE PRESTACIÓN DE SERVICIOS" in texto.upper()
        or "OBLIGACIONES DEL PROVEEDOR" in texto.upper()
        or "PRIMERA. OBJETO Y ALCANCE" in texto.upper()

    )


# ---------------- EXTRAER MINIMA CUANTIA ----------------

def extraer_info_minima_cuantia(pdf_path):

    texto = extraer_texto(pdf_path)
    texto = texto.strip()

    texto = re.sub(
        r"\b\d+\s+En\s+los\s+casos\s+que\s+aplique\.?",
        "",
        texto,
        flags=re.IGNORECASE
    )

    texto = re.sub(
        r"CIA-FO-\d+\s+\d{4}-\d{2}-\d{2}\s+V\d+",
        "",
        texto,
        flags=re.IGNORECASE
    )

    nombre, cedula = None, None

    match_nombre = re.search(
        r"(?:parte\s+)?([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+\s+[A-ZÁÉÍÓÚÑA-Za-zÁÉÍÓÚÑáéíóúñ\s]{3,})\s+(?:identificado|identificada|reconocido|portador)",
        texto,
        re.IGNORECASE
    )

    if match_nombre:
        nombre = match_nombre.group(1).strip()

        # limpiar frases comunes antes del nombre
        nombre = re.sub(
            r'^(?:y\s+)?(?:por\s+)?(?:otra\s+)?(?:parte\s+)?',
            '',
            nombre,
            flags=re.IGNORECASE
        ).strip()

    match_cedula = re.search(
        r"(?:C\.?C\.?|cédula\s+de\s+ciudadanía)[^\d]{0,10}([\d\.]{6,15})",
        texto,
        re.IGNORECASE
    )

    if match_cedula:
        cedula = match_cedula.group(1)

    # OBJETO

    objeto = None

    match_objeto = re.search(
        r"Primera\.\s*Objeto\s*y\s*alcance\.(.*?)(?=Segunda\.)",
        texto,
        re.IGNORECASE | re.DOTALL
    )

    if match_objeto:

        objeto = normalizar_parrafos(match_objeto.group(1).strip())

    # FECHAS

    fecha_inicio = None
    fecha_fin = None

    match_rango = re.search(
        r"del\s+(\d{1,2}\s+de\s+\w+)\s+a\s+(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})",
        texto,
        re.IGNORECASE
    )

    if match_rango:

        fecha_inicio = match_rango.group(1)
        fecha_fin = match_rango.group(2)

    # VALOR

    valor = None

    match_valor = re.search(
        r"\(\$\s*([\d\.\,]+)\)",
        texto
    )

    if match_valor:

        numero = match_valor.group(1).replace(".", "").replace(",", "")
        valor = "${:,.2f}".format(float(numero))

    # OBLIGACIONES

    funciones = []
    funciones_intro = ""

    match_obligaciones = re.search(
        r"OBLIGACIONES\s+DEL\s+PROVEEDOR:?(.+?)(?=CL[ÁA]USULA\s+SEXTA|$)",
        texto,
        re.IGNORECASE | re.DOTALL
    )

    if match_obligaciones:

        bloque = match_obligaciones.group(1)

        bloque = re.sub(r'\n+', ' ', bloque)

        bloque = re.sub(r'\.(\d+\.)', r'. \1', bloque)

        lista = re.findall(r'\d+\.\s*(.*?)(?=\s*\d+\.|$)', bloque)

        funciones = [
            normalizar_parrafos(item.strip())
            for item in lista if item.strip()
        ]

        funciones_intro = "Obligaciones del PROVEEDOR:"

    return {

        "Contratista Nombre": nombre or "No encontrado",
        "Contratista Cédula": cedula or "No encontrada",
        "CLÁUSULA PRIMERA. OBJETO": objeto or "No encontrada",
        "Intro Cláusula Quinta": funciones_intro,
        "Cláusula Quinta - Obligaciones": funciones,
        "Fecha inicio": fecha_inicio or "No encontrada",
        "Fecha fin": fecha_fin or "No encontrada",
        "Valor": valor or "No encontrado"

    }


# ---------------- EXTRAER CONTRATO NORMAL ----------------

def extraer_info_contrato(pdf_path):

    texto = extraer_texto(pdf_path)

    if es_minima_cuantia(texto):
        return extraer_info_minima_cuantia(pdf_path)

    contratista_nombre = None
    contratista_cedula = None

    # ---------------- TABLA PRINCIPAL ----------------

    with pdfplumber.open(pdf_path) as pdf:

        tablas = pdf.pages[0].extract_tables()

        if tablas:

            tabla = tablas[0]

            for fila in tabla:

                if fila and len(fila) >= 2:

                    if "CONTRATISTA" in (fila[0] or "").upper():

                        valor = fila[1] or ""

                        partes = valor.split(",")

                        # NOMBRE

                        if len(partes) > 0:

                            contratista_nombre = partes[0].strip()

                        # CEDULA (después de primera coma)

                        if len(partes) > 1:

                            posible = partes[1]

                            match_cedula = re.search(r"([\d\.]{6,15})", posible)

                            if match_cedula:

                                contratista_cedula = match_cedula.group(1)#.replace(".", "")

    # ---------------- OBJETO ----------------

    objeto = None

    match_objeto = re.search(

        r"CLÁUSULA\s+PRIMERA.*?OBJETO[:\s-]*(.*?)(?=CLÁUSULA\s+SEGUNDA)",

        texto,
        re.IGNORECASE | re.DOTALL

    )

    if match_objeto:

        objeto = normalizar_parrafos(match_objeto.group(1).strip())

    # ---------------- OBLIGACIONES ----------------

    funciones = []
    funciones_intro = ""

    match_funciones = re.search(

        r"CLÁUSULA\s+TERCERA.*?OBLIGACIONES.*?(.*?)(?=CLÁUSULA\s+CUARTA)",

        texto,
        re.IGNORECASE | re.DOTALL

    )

    if match_funciones:

        bloque = match_funciones.group(1)

        lista = re.findall(r'\d+\.\s*(.*?)(?=\d+\.|$)', bloque, re.DOTALL)

        funciones = [

            normalizar_parrafos(x.strip())
            for x in lista if x.strip()

        ]

        funciones_intro = "Dentro del objeto del contrato, EL CONTRATISTA se obliga para con LA UNIVERSIDAD a realizar las siguientes actividades:"

    # ---------------- VALOR ----------------

    # ---------------- VALOR ----------------

    valor = None

    # primero intenta sacar el valor desde la tabla
    with pdfplumber.open(pdf_path) as pdf:

        tablas = pdf.pages[0].extract_tables()

        if tablas:

            tabla = tablas[0]

            for fila in tabla:

                if fila and len(fila) >= 2:

                    if "VALOR DEL CONTRATO" in (fila[0] or "").upper():

                        valor = fila[1].strip()

    # si no lo encuentra en tabla usa regex
    if not valor:

        match_valor = re.search(
            r"VALOR\s+DEL\s+CONTRATO\s*(.*?)\n",
            texto,
            re.IGNORECASE
        )

        if match_valor:

            valor = match_valor.group(1).strip()

    # ---------------- FECHAS ----------------

    fecha_inicio = None
    fecha_fin = None

    match_fechas = re.search(

        r"(?:Del\s+)?(\d{1,2}\s+de?\s+\w+\s+(?:de|del)?\s*\d{4})\s+al\s+(\d{1,2}\s+de?\s+\w+\s+(?:de|del)?\s*\d{4})",
        
        texto,
        re.IGNORECASE

    )

    if match_fechas:

        fecha_inicio = match_fechas.group(1).replace(" del ", " de ")
        fecha_fin = match_fechas.group(2).replace(" del ", " de ")

    return {

        "Contratista Nombre": contratista_nombre or "No encontrado",
        "Contratista Cédula": contratista_cedula or "No encontrada",
        "CLÁUSULA PRIMERA. OBJETO": objeto or "No encontrada",
        "Intro Cláusula Tercera": funciones_intro,
        "Cláusula Tercera - Obligaciones": funciones,
        "Fecha inicio": fecha_inicio or "No encontrada",
        "Fecha fin": fecha_fin or "No encontrada",
        "Valor": valor or "No encontrado"

    }

# ---------------- RUTAS FLASK ----------------

@app.route("/")
def inicio():
    return render_template("inicio.html")


@app.route("/formulario")
def formulario():
    return render_template("index.html")


@app.route("/minCuantia")
def minCuantia():
    return render_template("index_mc.html")


# ---------------- EXTRAER CONTRATO ----------------

@app.route("/extraer_contrato", methods=["POST"])
def extraer_contrato():

    contrato_file = request.files.get("contrato")

    if not contrato_file:
        return jsonify({"error": "Debe subir el contrato"}), 400

    contrato_path = "temp_" + contrato_file.filename
    contrato_file.save(contrato_path)

    datos = extraer_info_contrato(contrato_path)

    os.remove(contrato_path)

    return render_template("resultado.html", datos=datos)


# ---------------- EXTRAER MINIMA CUANTIA ----------------

@app.route("/extraer_mc", methods=["POST"])
def extraer_mc():

    contrato_file = request.files.get("contrato")

    if not contrato_file:
        return jsonify({"error": "Debe subir el contrato"}), 400

    contrato_path = "temp_" + contrato_file.filename
    contrato_file.save(contrato_path)

    datos = extraer_info_minima_cuantia(contrato_path)

    os.remove(contrato_path)

    return render_template("resultado_mc.html", datos=datos)


# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(debug=True)