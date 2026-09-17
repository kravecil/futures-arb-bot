# ---- Сборка ----
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY Directory.Build.props Directory.Packages.props global.json ./
COPY FuturesArbBot.slnx ./
COPY src/FuturesArbBot.Core/FuturesArbBot.Core.csproj            src/FuturesArbBot.Core/
COPY src/FuturesArbBot.Infrastructure/FuturesArbBot.Infrastructure.csproj src/FuturesArbBot.Infrastructure/
COPY src/FuturesArbBot.App/FuturesArbBot.App.csproj              src/FuturesArbBot.App/
COPY tests/FuturesArbBot.Tests/FuturesArbBot.Tests.csproj        tests/FuturesArbBot.Tests/
RUN dotnet restore

COPY . .
RUN dotnet publish src/FuturesArbBot.App -c Release -o /app --no-restore

# ---- Рантайм ----
FROM mcr.microsoft.com/dotnet/runtime:10.0
WORKDIR /app
COPY --from=build /app .
COPY config/ /app/config/

ENTRYPOINT ["dotnet", "FuturesArbBot.dll"]
