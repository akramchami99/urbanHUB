import pandas as pd
import streamlit as st
from urbanhub_common.iceberg import inventory, catalog, io_properties, scan_table
from pyiceberg.io.pyarrow import PyArrowFileIO

st.set_page_config(page_title="Data Lake Iceberg · UrbanHub",page_icon="🧊",layout="wide")
st.title("🧊 Apache Iceberg : catalogue et snapshots")
st.caption("MinIO stocke les fichiers Parquet, manifestes Avro et métadonnées JSON Iceberg. "
           "Le catalogue REST Nessie conserve les références des tables dans son volume RocksDB.")

@st.cache_data(ttl=30)
def tables():
    return pd.DataFrame(inventory())

try:
    frame=tables()
except Exception as exc:
    st.error(f"Catalogue Iceberg indisponible : {exc}")
    st.stop()
if frame.empty:
    st.info("Aucune table publiée. Lancer la migration ou une ingestion.")
    st.stop()
st.metric("Tables Iceberg",len(frame))
st.dataframe(frame,hide_index=True,use_container_width=True)
selected=st.selectbox("Table",frame.table.tolist())
table=catalog().load_table(tuple(selected.split(".")))
table.io=PyArrowFileIO(io_properties())
snapshot=table.current_snapshot()
a,b,c=st.columns(3)
a.metric("Format Iceberg",f"v{table.metadata.format_version}")
b.metric("Schéma",table.schema().schema_id)
c.metric("Snapshot courant",str(snapshot.snapshot_id) if snapshot else "vide")
st.code(table.metadata_location,language=None)
st.markdown("**Schéma de la table**")
st.dataframe(pd.DataFrame([dict(id=f.field_id,colonne=f.name,type=str(f.field_type),requis=f.required)
                          for f in table.schema().fields]),hide_index=True,use_container_width=True)
st.markdown("**Partitionnement**")
st.code(str(table.spec()),language=None)
st.markdown("**Snapshot exposé par Nessie**")
st.dataframe(table.inspect.snapshots().to_pandas(),hide_index=True,use_container_width=True)
st.caption("Nessie expose le snapshot de la version du catalogue consultée. Les métadonnées déjà "
           "committées restent dans MinIO et permettent une lecture reproductible d'une ancienne version.")
with st.expander("Lire une ancienne version Iceberg conservée"):
    location=st.text_input("URI S3 du fichier .metadata.json committé",key="iceberg_old_metadata")
    if st.button("Lire cette version"):
        if not location.startswith(table.location().rstrip("/")+"/metadata/") or not location.endswith(".metadata.json"):
            st.error("Choisir un fichier de métadonnées de cette table.")
        else:
            data=scan_table(table.properties["urbanhub.dataset"],table.properties["urbanhub.zone"],metadata_location=location)
            st.write(f"{len(data):,} lignes dans cette version")
            st.dataframe(data.head(100),hide_index=True,use_container_width=True)
